#include "crash_reporter.h"

// The POSIX crash reporter: a handler for the fatal signals, on its own stack,
// writes <crash_root>/<session>-<signal>.txt with the signal, the faulting
// address, the program counter and a backtrace, then lets the default action
// end the process (and leave a core dump where the system keeps them). Only
// async-signal-safe calls run in the handler, except backtrace(), which glibc
// and macOS support there once it has been called before the fault.
//
// The runtime takes SIGSEGV for its own work too: guest MMIO accesses and
// writes to watched GPU memory fault on purpose and its handler resolves
// them. Unlike a Windows unhandled-exception filter, sigaction replaces that
// handler, so ours keeps the one it replaced and passes faults to it first.
// A fault is a crash only when the same instruction keeps faulting on the
// same address, which is what an unresolved fault does once the handler
// returns. A resolved one can repeat too: another thread (a GPU upload of the
// same memory) may watch the page again before the instruction retries, which
// has been seen 31 times in a row, so the bar is many repeats.

#include <fcntl.h>
#include <signal.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <iterator>

#if defined(__GLIBC__) || defined(__APPLE__)
#include <execinfo.h>
#define PINYON_SHIFT_HAVE_BACKTRACE 1
#endif

namespace pinyon_shift::diagnostics::crash {
namespace {

constexpr int kSignals[] = {SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGABRT};
// "<crash_root>/<session>-" prepared at install; the handler appends the
// signal name, so it never allocates.
std::array<char, 4096> g_report_prefix{};
size_t g_report_prefix_length = 0;
std::atomic_flag g_reported = ATOMIC_FLAG_INIT;
// Roomy enough for the runtime's handler, which runs here when it is chained.
std::array<std::byte, 256 * 1024> g_signal_stack{};
// The action each of kSignals had before ours, called first for faults.
struct sigaction g_previous[std::size(kSignals)]{};
// Faults in a row at the same instruction and address before one is a crash.
// Each legitimate repeat costs the other thread an upload and a new watch,
// so this many take seconds; a fault nothing resolves gets here in
// milliseconds. (Waiting for seconds as well, as this once did, only let a
// real crash spin and log for that long first.)
constexpr unsigned kRepeatedFaultsFatal = 1000;

struct LastFault {
  int signal = 0;
  uint64_t pc = 0;
  uintptr_t address = 0;
  unsigned count = 0;
};

thread_local LastFault t_last_fault;

const char* SignalName(int signal) {
  switch (signal) {
    case SIGSEGV:
      return "sigsegv";
    case SIGBUS:
      return "sigbus";
    case SIGILL:
      return "sigill";
    case SIGFPE:
      return "sigfpe";
    case SIGABRT:
      return "sigabrt";
    default:
      return "signal";
  }
}

void Write(int file, const char* text) { (void)!write(file, text, std::strlen(text)); }

void WriteHex(int file, const char* label, uint64_t value) {
  char buffer[40];
  size_t length = 0;
  for (const char* c = label; *c && length < 16; ++c) buffer[length++] = *c;
  buffer[length++] = '0';
  buffer[length++] = 'x';
  for (int shift = 60; shift >= 0; shift -= 4) {
    buffer[length++] = "0123456789ABCDEF"[(value >> shift) & 0xF];
  }
  buffer[length++] = '\n';
  (void)!write(file, buffer, length);
}

uint64_t ParseHex(const char*& c) {
  uint64_t value = 0;
  for (;; ++c) {
    if (*c >= '0' && *c <= '9') {
      value = value * 16 + uint64_t(*c - '0');
    } else if (*c >= 'a' && *c <= 'f') {
      value = value * 16 + uint64_t(*c - 'a' + 10);
    } else {
      return value;
    }
  }
}

// The /proc/self/maps line covering `address`, with its protection, read
// with plain system calls.
void WriteMapping(int file, uintptr_t address) {
#if defined(__linux__)
  const int maps = open("/proc/self/maps", O_RDONLY | O_CLOEXEC);
  if (maps < 0) return;
  char buffer[8192];
  char line[512];
  size_t line_length = 0;
  ssize_t count;
  bool found = false;
  while (!found && (count = read(maps, buffer, sizeof(buffer))) > 0) {
    for (ssize_t i = 0; i < count && !found; ++i) {
      if (buffer[i] != '\n') {
        if (line_length < sizeof(line) - 1) line[line_length++] = buffer[i];
        continue;
      }
      line[line_length] = '\0';
      const char* c = line;
      const uint64_t start = ParseHex(c);
      if (*c == '-') ++c;
      const uint64_t end = ParseHex(c);
      if (address >= start && address < end) {
        Write(file, "mapping=");
        line[line_length++] = '\n';
        (void)!write(file, line, line_length);
        found = true;
      }
      line_length = 0;
    }
  }
  if (!found) Write(file, "mapping=none\n");
  close(maps);
#else
  (void)file;
  (void)address;
#endif
}

uint64_t ProgramCounter(const void* context) {
  const auto* user = static_cast<const ucontext_t*>(context);
  if (!user) return 0;
#if defined(__APPLE__) && defined(__x86_64__)
  return user->uc_mcontext->__ss.__rip;
#elif defined(__APPLE__) && defined(__aarch64__)
  return user->uc_mcontext->__ss.__pc;
#elif defined(__linux__) && defined(__x86_64__)
  return uint64_t(user->uc_mcontext.gregs[REG_RIP]);
#elif defined(__linux__) && defined(__aarch64__)
  return user->uc_mcontext.pc;
#else
  return 0;
#endif
}

void Handler(int signal, siginfo_t* info, void* context);

bool IsOurs(const struct sigaction& action) {
  return (action.sa_flags & SA_SIGINFO) && action.sa_sigaction == Handler;
}

// Passes a fault the kernel raised to the handler ours replaced. Returns
// true when that handler resolved it, so the faulting thread can go on.
bool ForwardToPrevious(int signal, siginfo_t* info, void* context) {
  // Signals sent by raise() or kill() are not faults anything can resolve.
  if (!info || info->si_code <= 0) return false;
  size_t index = 0;
  while (index < std::size(kSignals) && kSignals[index] != signal) ++index;
  if (index == std::size(kSignals)) return false;
  const struct sigaction& previous = g_previous[index];
  const bool siginfo = (previous.sa_flags & SA_SIGINFO) != 0;
  if (siginfo ? previous.sa_sigaction == nullptr
              : (previous.sa_handler == SIG_DFL || previous.sa_handler == SIG_IGN)) {
    return false;
  }

  const uint64_t pc = ProgramCounter(context);
  const auto address = uintptr_t(info->si_addr);
  LastFault& last = t_last_fault;
  if (last.signal == signal && last.pc == pc && last.address == address) {
    ++last.count;
  } else {
    last = {signal, pc, address, 1};
  }
  if (last.count >= kRepeatedFaultsFatal) {
    return false;
  }

  if (siginfo) {
    previous.sa_sigaction(signal, info, context);
  } else {
    previous.sa_handler(signal);
  }
  // A handler that emulated the access moved past the instruction.
  if (ProgramCounter(context) != pc) last = {};
  return true;
}

void Handler(int signal, siginfo_t* info, void* context) {
  if (ForwardToPrevious(signal, info, context)) return;
  if (!g_reported.test_and_set()) {
    char path[4200];
    std::memcpy(path, g_report_prefix.data(), g_report_prefix_length);
    size_t length = g_report_prefix_length;
    for (const char* c = SignalName(signal); *c; ++c) path[length++] = *c;
    for (const char* c = ".txt"; *c; ++c) path[length++] = *c;
    path[length] = '\0';
    const int file = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (file >= 0) {
      Write(file, "Pinyon Shift fatal signal\nsignal=");
      Write(file, SignalName(signal));
      Write(file, "\n");
      WriteHex(file, "fault_address=", uint64_t(uintptr_t(info ? info->si_addr : nullptr)));
      WriteHex(file, "pc=", ProgramCounter(context));
      // SEGV_MAPERR (1): nothing mapped there; SEGV_ACCERR (2): protection.
      WriteHex(file, "si_code=", uint64_t(uint32_t(info ? info->si_code : 0)));
      WriteMapping(file, uintptr_t(info ? info->si_addr : nullptr));
#if defined(PINYON_SHIFT_HAVE_BACKTRACE)
      void* frames[64];
      const int count = backtrace(frames, 64);
      backtrace_symbols_fd(frames, count, file);
#endif
      close(file);
    }
  }
  // The default action ends the process as the signal would have.
  struct sigaction default_action {};
  default_action.sa_handler = SIG_DFL;
  sigemptyset(&default_action.sa_mask);
  sigaction(signal, &default_action, nullptr);
  raise(signal);
}

}  // namespace

void Install(const std::filesystem::path& crash_root, const std::string& session_id) {
  const std::string prefix = (crash_root / session_id).string() + "-";
  g_report_prefix_length = std::min(prefix.size(), g_report_prefix.size() - 1);
  std::memcpy(g_report_prefix.data(), prefix.data(), g_report_prefix_length);
#if defined(PINYON_SHIFT_HAVE_BACKTRACE)
  // The first backtrace() loads the unwinder; do it now, not in the handler.
  void* frame;
  backtrace(&frame, 1);
#endif
  stack_t stack{};
  stack.ss_sp = g_signal_stack.data();
  stack.ss_size = g_signal_stack.size();
  sigaltstack(&stack, nullptr);
  Refresh();
}

void Refresh() {
  struct sigaction action {};
  action.sa_sigaction = Handler;
  action.sa_flags = SA_SIGINFO | SA_ONSTACK;
  sigemptyset(&action.sa_mask);
  for (size_t i = 0; i < std::size(kSignals); ++i) {
    struct sigaction replaced {};
    sigaction(kSignals[i], &action, &replaced);
    // Refreshing again must not make ours its own predecessor.
    if (!IsOurs(replaced)) g_previous[i] = replaced;
  }
}

void RaiseAccessViolation() { raise(SIGSEGV); }

void ExecuteNull() {
  volatile uintptr_t null_target = 0;
  reinterpret_cast<void (*)()>(null_target)();
}

}  // namespace pinyon_shift::diagnostics::crash
