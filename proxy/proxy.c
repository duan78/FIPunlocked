/*
** proxy.c — DLL proxy pour DirectOutputSaiFip.dll (fipd, RE 19/11/2026)
** Charge real\DirectOutputSaiFip.dll, loggue CHAQUE appel Driver_* (args + chaines)
** dans C:\ProgramData\fipd_proxy_log.txt, et transmet tel quel.
** Build : gcc -shared -o DirectOutputSaiFip.dll proxy.c
*/
#include <windows.h>
#include <stdio.h>
#include <stdarg.h>

#define LOG_PATH "C:\\ProgramData\\fipd_proxy_log.txt"
#define NEXPORTS 26

static const char* EXPORTS[NEXPORTS] = {
    "DirectOutputDriver_GetInterfaceGuid", "DirectOutputDriver_CanCreate",
    "DirectOutputDriver_Create", "DirectOutputDriver_Destroy",
    "DirectOutputDriver_GetTypeGuid", "DirectOutputDriver_GetIsVisible",
    "DirectOutputDriver_RegisterSoftButtonCallback", "DirectOutputDriver_SetProfile",
    "DirectOutputDriver_ClearDisplay", "DirectOutputDriver_SetLed",
    "DirectOutputDriver_SetString", "DirectOutputDriver_SetImage",
    "DirectOutputDriver_SetImageFile", "DirectOutputDriver_SaveFile",
    "DirectOutputDriver_DisplayFile", "DirectOutputDriver_DeleteFile",
    "DirectOutputDriver_StartServer", "DirectOutputDriver_CloseServer",
    "DirectOutputDriver_SendServerMsg", "DirectOutputDriver_SendServerFile",
    "DirectOutputDriver_FolderRemoved",
    "DirectOutputDriver_DefaultPageRegisterForceActivateCallback",
    "DirectOutputDriver_DefaultPageActivate", "DirectOutputDriver_DefaultPageSoftButton",
    "DirectOutputDriver_SetNumberOfPages", "DirectOutputDriver_GetSerialNumber",
};

typedef long long (*pfn12)(long long, long long, long long, long long, long long,
                           long long, long long, long long, long long, long long,
                           long long, long long);
static pfn12 g_real[NEXPORTS];
static HMODULE g_hreal;
static CRITICAL_SECTION g_cs;

static void logf_(const char* fmt, ...)
{
    FILE* f;
    va_list ap;
    SYSTEMTIME st;
    EnterCriticalSection(&g_cs);
    f = fopen(LOG_PATH, "a");
    if (f) {
        GetLocalTime(&st);
        fprintf(f, "%02u:%02u:%02u.%03u ", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
        va_start(ap, fmt);
        vfprintf(f, fmt, ap);
        va_end(ap);
        fprintf(f, "\n");
        fclose(f);
    }
    LeaveCriticalSection(&g_cs);
}

/* conversion wchar -> utf8 locale C hors危 (ASCII only, suffit pour des chemins) */
static void w2a(const wchar_t* w, unsigned int cch, char* out, size_t outsz)
{
    size_t i;
    if (!w) { snprintf(out, outsz, "(null)"); return; }
    for (i = 0; i + 1 < outsz && i < cch && w[i]; i++)
        out[i] = (w[i] < 128) ? (char)w[i] : '?';
    out[i] = '\0';
}

static void dump_str_arg(const char* name, int argno, long long v, long long cch)
{
    char buf[600];
    if (!v) { logf_("    %s.arg%d = NULL", name, argno); return; }
    w2a((const wchar_t*)(uintptr_t)v, (unsigned int)(cch ? cch : 260), buf, sizeof buf);
    logf_("    %s.arg%d = \"%s\"", name, argno, buf);
}

/* --- macro : wrapper generique 12 args --- */
#define WRAP(idx, name)                                                  \
__declspec(dllexport) long long name(long long a1, long long a2, long long a3, \
                                     long long a4, long long a5, long long a6, \
                                     long long a7, long long a8, long long a9, \
                                     long long a10, long long a11, long long a12) \
{                                                                       \
    long long r;                                                        \
    if (!g_real[idx]) { logf_(#name " NON RESOLU"); return -1; }        \
    r = g_real[idx](a1,a2,a3,a4,a5,a6,a7,a8,a9,a10,a11,a12);           \
    logf_(#name "(%llX,%llX,%llX,%llX,%llX,%llX,%llX,%llX,%llX,%llX,%llX,%llX) -> %llX", \
          a1,a2,a3,a4,a5,a6,a7,a8,a9,a10,a11,a12, r);                  \
    return r;                                                           \
}

/* wrappers specialises : decodent les chaines / valeurs cles */
__declspec(dllexport) long long DirectOutputDriver_Create(
    long long cch, long long wpath, long long ph, long long a4, long long a5,
    long long a6, long long a7, long long a8, long long a9, long long a10,
    long long a11, long long a12)
{
    long long r = g_real[2](cch, wpath, ph, a4, a5, a6, a7, a8, a9, a10, a11, a12);
    char buf[600];
    w2a((const wchar_t*)(uintptr_t)wpath, (unsigned int)cch, buf, sizeof buf);
    logf_("CREATE cch=%lld path=\"%s\" hr=%llX handle=%p", cch, buf, r,
          ph ? *(void**)(uintptr_t)ph : NULL);
    return r;
}

__declspec(dllexport) long long DirectOutputDriver_StartServer(
    long long h, long long cch, long long wpath, long long pid, long long pst,
    long long a6, long long a7, long long a8, long long a9, long long a10,
    long long a11, long long a12)
{
    long long r = g_real[16](h, cch, wpath, pid, pst, a6, a7, a8, a9, a10, a11, a12);
    char buf[600];
    w2a((const wchar_t*)(uintptr_t)wpath, (unsigned int)cch, buf, sizeof buf);
    logf_("*** STARTSERVER h=%llX cch=%lld file=\"%s\" serverId=%llX hr=%llX", h, cch,
          buf, pid ? *(long long*)(uintptr_t)pid : -1, r);
    return r;
}

__declspec(dllexport) long long DirectOutputDriver_SetProfile(
    long long h, long long cch, long long wpath, long long a4, long long a5,
    long long a6, long long a7, long long a8, long long a9, long long a10,
    long long a11, long long a12)
{
    long long r = g_real[7](h, cch, wpath, a4, a5, a6, a7, a8, a9, a10, a11, a12);
    dump_str_arg("SETPROFILE", 2, wpath, cch);
    return r;
}

__declspec(dllexport) long long DirectOutputDriver_SetImage(
    long long h, long long page, long long idx, long long cb, long long pv,
    long long a6, long long a7, long long a8, long long a9, long long a10,
    long long a11, long long a12)
{
    long long r = g_real[11](h, page, idx, cb, pv, a6, a7, a8, a9, a10, a11, a12);
    logf_("SETIMAGE h=%llX page=%lld idx=%lld cb=%lld hr=%llX", h, page, idx, cb, r);
    return r;
}

/* les autres : generiques */
WRAP(0,  DirectOutputDriver_GetInterfaceGuid)
WRAP(1,  DirectOutputDriver_CanCreate)
WRAP(3,  DirectOutputDriver_Destroy)
WRAP(4,  DirectOutputDriver_GetTypeGuid)
WRAP(5,  DirectOutputDriver_GetIsVisible)
WRAP(6,  DirectOutputDriver_RegisterSoftButtonCallback)
WRAP(8,  DirectOutputDriver_ClearDisplay)
WRAP(9,  DirectOutputDriver_SetLed)
WRAP(10, DirectOutputDriver_SetString)
WRAP(12, DirectOutputDriver_SetImageFile)
WRAP(13, DirectOutputDriver_SaveFile)
WRAP(14, DirectOutputDriver_DisplayFile)
WRAP(15, DirectOutputDriver_DeleteFile)
WRAP(17, DirectOutputDriver_CloseServer)
WRAP(18, DirectOutputDriver_SendServerMsg)
WRAP(19, DirectOutputDriver_SendServerFile)
WRAP(20, DirectOutputDriver_FolderRemoved)
WRAP(21, DirectOutputDriver_DefaultPageRegisterForceActivateCallback)
WRAP(22, DirectOutputDriver_DefaultPageActivate)
WRAP(23, DirectOutputDriver_DefaultPageSoftButton)
WRAP(24, DirectOutputDriver_SetNumberOfPages)
WRAP(25, DirectOutputDriver_GetSerialNumber)

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID rsv)
{
    wchar_t self[MAX_PATH];
    int i;
    (void)rsv;
    if (reason != DLL_PROCESS_ATTACH)
        return TRUE;
    DisableThreadLibraryCalls(h);
    InitializeCriticalSection(&g_cs);
    GetModuleFileNameW(h, self, MAX_PATH);
    /* remplacer le nom de fichier par real\DirectOutputSaiFip.dll */
    for (i = (int)wcslen(self) - 1; i >= 0 && self[i] != L'\\'; i--) {}
    if (i >= 0) {
        self[i + 1] = 0;
        wcscat(self, L"real\\DirectOutputSaiFip.dll");
        g_hreal = LoadLibraryW(self);
    }
    if (!g_hreal)
        g_hreal = LoadLibraryW(L"real\\DirectOutputSaiFip.dll");
    if (!g_hreal) {
        logf_("PROXY: impossible de charger real\\DirectOutputSaiFip.dll (err=%lu)",
              GetLastError());
        return FALSE;
    }
    for (i = 0; i < NEXPORTS; i++) {
        g_real[i] = (pfn12)(uintptr_t)GetProcAddress(g_hreal, EXPORTS[i]);
        if (!g_real[i])
            logf_("PROXY: export manquant %s", EXPORTS[i]);
    }
    logf_("PROXY charge, vrai plugin=%p", (void*)g_hreal);
    return TRUE;
}
