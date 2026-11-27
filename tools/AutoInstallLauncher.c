/* Minimal native Windows launcher. No C runtime or Python dependency. */
typedef void *HWND;
typedef void *HINSTANCE;
typedef unsigned long DWORD;
typedef unsigned short wchar_t;

__declspec(dllimport) DWORD __stdcall GetModuleFileNameW(
    HINSTANCE module, wchar_t *buffer, DWORD size);
__declspec(dllimport) void __stdcall ExitProcess(unsigned int code);
__declspec(dllimport) HINSTANCE __stdcall ShellExecuteW(
    HWND window, const wchar_t *operation, const wchar_t *file,
    const wchar_t *parameters, const wchar_t *directory, int show);
__declspec(dllimport) int __stdcall MessageBoxW(
    HWND window, const wchar_t *text, const wchar_t *caption, unsigned int type);

static wchar_t path[4096];
#ifdef LIVE_CHECK
static const wchar_t script[] = L"Live_Check.bat";
#elif defined(QUICK_CHECK)
static const wchar_t script[] = L"Quick_Check.bat";
#else
static const wchar_t script[] = L"Start_AutoInstall.bat";
#endif

void entry(void) {
    DWORD length = GetModuleFileNameW(0, path, 4096);
    int slash = -1;
    DWORD i;
    HINSTANCE result;

    if (length == 0 || length >= 4096) goto failed;
    for (i = 0; i < length; ++i) {
        if (path[i] == L'\\' || path[i] == L'/') slash = (int)i;
    }
    if (slash < 0 || (DWORD)(slash + 1) +
            (DWORD)(sizeof(script) / sizeof(script[0])) > 4096) goto failed;
    for (i = 0; i < sizeof(script) / sizeof(script[0]); ++i) {
        path[slash + 1 + i] = script[i];
    }
    result = ShellExecuteW(0, L"open", path, 0, 0, 1);
    if ((unsigned long long)result <= 32) goto failed;
    ExitProcess(0);

failed:
    MessageBoxW(0, L"Cannot start the companion script. Keep the whole folder together.",
                L"KK Auto", 0x10);
    ExitProcess(1);
}
