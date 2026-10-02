// The POSIX crash reporter (src/crash_reporter_posix.cpp) sits over the
// runtime's own SIGSEGV handler, as in the app: faults that handler resolves,
// at once or after another thread watched the page again, must go on without
// a report; faults nothing resolves, raised signals and wild calls must be
// reported. Each case runs in a child process.

#include "crash_reporter.h"

#include <signal.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>

namespace crash = pinyon_shift::diagnostics::crash;

namespace {

int failures = 0;

#define CHECK(condition)                                                        \
  do {                                                                          \
    if (!(condition)) {                                                         \
      std::fprintf(stderr, "%s:%d: check failed: %s\n", __FILE__, __LINE__,   \
                   #condition);                                                 \
      ++failures;                                                               \
    }                                                                           \
  } while (false)

int* g_page = nullptr;
size_t g_page_size = 0;
int g_attempts = 0;
// The fault on which the stand-in runtime handler lifts the watch; 0: never.
int g_resolve_on_attempt = 0;

// Stands in for the runtime's handler: a write watch on g_page, lifted by
// making the page writable on the chosen attempt. Earlier attempts leave it
// read-only, as when another thread watches the page again before the write
// retries.
void RuntimeHandler(int, siginfo_t* info, void*) {
  ++g_attempts;
  if (info->si_addr == g_page && g_resolve_on_attempt &&
      g_attempts >= g_resolve_on_attempt) {
    mprotect(g_page, g_page_size, PROT_READ | PROT_WRITE);
  }
}

// The app's order: the reporter early, the runtime's handler at setup, then
// the reporter refreshed twice (after setup and before the module launches).
int RunChild(const std::filesystem::path& root, const char* session, void (*body)()) {
  const pid_t pid = fork();
  if (pid == 0) {
    crash::Install(root, session);
    struct sigaction action {};
    action.sa_sigaction = RuntimeHandler;
    action.sa_flags = SA_SIGINFO;
    sigemptyset(&action.sa_mask);
    sigaction(SIGSEGV, &action, nullptr);
    crash::Refresh();
    crash::Refresh();
    body();
    _exit(0);
  }
  int status = 0;
  waitpid(pid, &status, 0);
  return status;
}

void WriteWatchedPage() {
  *static_cast<volatile int*>(g_page) = 1;
}

bool ExitedNormally(int status) { return WIFEXITED(status) && WEXITSTATUS(status) == 0; }

bool DiedOfSegv(int status) { return WIFSIGNALED(status) && WTERMSIG(status) == SIGSEGV; }

std::string ReadFile(const std::filesystem::path& path) {
  std::ifstream input(path);
  std::stringstream text;
  text << input.rdbuf();
  return text.str();
}

}  // namespace

int main() {
  g_page_size = size_t(sysconf(_SC_PAGESIZE));
  g_page = static_cast<int*>(
      mmap(nullptr, g_page_size, PROT_READ, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0));
  CHECK(g_page != MAP_FAILED);
  const auto root = std::filesystem::temp_directory_path() /
                    ("pinyon-crash-test-" + std::to_string(getpid()));
  std::filesystem::create_directories(root);

  g_resolve_on_attempt = 1;
  CHECK(ExitedNormally(RunChild(root, "resolved", WriteWatchedPage)));
  CHECK(!std::filesystem::exists(root / "resolved-sigsegv.txt"));

  g_resolve_on_attempt = 8;
  CHECK(ExitedNormally(RunChild(root, "rewatched", WriteWatchedPage)));
  CHECK(!std::filesystem::exists(root / "rewatched-sigsegv.txt"));

  // Hundreds of rounds, well inside the reporter's bar; the runtime's GPU
  // re-watch ping-pong was seen going to 31.
  g_resolve_on_attempt = 800;
  CHECK(ExitedNormally(RunChild(root, "contended", WriteWatchedPage)));
  CHECK(!std::filesystem::exists(root / "contended-sigsegv.txt"));

  g_resolve_on_attempt = 0;
  CHECK(DiedOfSegv(RunChild(root, "unresolved", WriteWatchedPage)));
  const std::string report = ReadFile(root / "unresolved-sigsegv.txt");
  CHECK(report.find("signal=sigsegv") != std::string::npos);
  CHECK(report.find("si_code=0x0000000000000002") != std::string::npos);
  CHECK(report.find("mapping=") != std::string::npos);

  CHECK(DiedOfSegv(RunChild(root, "raised", crash::RaiseAccessViolation)));
  CHECK(std::filesystem::exists(root / "raised-sigsegv.txt"));

  CHECK(DiedOfSegv(RunChild(root, "null", crash::ExecuteNull)));
  CHECK(std::filesystem::exists(root / "null-sigsegv.txt"));

  std::filesystem::remove_all(root);
  if (failures) {
    std::fprintf(stderr, "crash_reporter: %d check(s) failed\n", failures);
    return 1;
  }
  std::puts("crash_reporter tests passed");
  return 0;
}
