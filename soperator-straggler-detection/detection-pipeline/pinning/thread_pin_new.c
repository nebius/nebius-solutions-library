/* thread_pin_new.c -- NEW, UNVALIDATED LD_PRELOAD interposer (written from
 * scratch; NOT the original thread_pin.cc, which could not be located).
 *
 * Pins two roles to dedicated CPUs by wrapping pthread_create:
 *   dump     : Inspector plugin's dump thread. Identified by dladdr() of the
 *              start routine: its shared object path contains PIN_DUMP_MATCH
 *              (default "profiler-inspector"). Inspector calls pthread_create
 *              directly from the plugin .so.
 *   profiler : NCCL's own profiler thread (ncclProfilerThreadCreate, NCCL
 *              >=2.32.3). It is a std::thread, so the start routine is
 *              libstdc++'s trampoline; instead the CALL STACK is checked for a
 *              return address inside the address range of
 *              ncclProfilerThreadCreate, given as PIN_PROFILER_RANGES
 *              ("module-substr:0xSTART:0xSIZE[,...]", offsets in the module's
 *              file; produced by rank_setup.py from the .so's symtab).
 *              If NCCL has no such symbol (e.g. 2.30.1) no range is given and
 *              the profiler role reports UNAVAILABLE -- never silently skipped.
 * Pinning is done inside a trampoline running ON the new thread, which then
 * reads the affinity back (sched_getaffinity) and appends a record to
 * $PIN_LOG_DIR/pin_<host>_<pid>.log. An independent check reads /proc
 * (verify_pinning.py).
 * Env: PIN_ENABLE=1 PIN_DUMP_CPUS=a,b PIN_PROFILER_CPUS=c,d PIN_LOG_DIR=dir
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <sched.h>
#include <dlfcn.h>
#include <link.h>
#include <execinfo.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/syscall.h>
#include <time.h>

typedef int (*pc_fn)(pthread_t *, const pthread_attr_t *, void *(*)(void *), void *);
static pc_fn real_pc;
static int enabled, logfd = -1;
static cpu_set_t dump_set, prof_set;
static int have_dump, have_prof;
static const char *dump_match = "profiler-inspector";
#define MAXR 8
static struct { char mod[128]; unsigned long start, size; } ranges[MAXR];
static int nranges;

static int parse_cpus(const char *s, cpu_set_t *set) {
  CPU_ZERO(set); int n = 0; if (!s || !*s) return 0;
  char *c = strdup(s), *sv, *t;
  for (t = strtok_r(c, ",", &sv); t; t = strtok_r(NULL, ",", &sv)) {
    int a, b; if (sscanf(t, "%d-%d", &a, &b) == 2) { for (int i = a; i <= b && i < CPU_SETSIZE; i++) { CPU_SET(i, set); n++; } }
    else { a = atoi(t); if (a >= 0 && a < CPU_SETSIZE) { CPU_SET(a, set); n++; } }
  }
  free(c); return n;
}
static void rec(const char *fmt, ...) __attribute__((format(printf,1,2)));
static void rec(const char *fmt, ...) {
  if (logfd < 0) return;
  char buf[512];
  __builtin_va_list ap; __builtin_va_start(ap, fmt);
  int n = vsnprintf(buf, sizeof buf, fmt, ap); __builtin_va_end(ap);
  if (n > 0) { if (write(logfd, buf, n < (int)sizeof buf ? n : (int)sizeof buf - 1) < 0) {} }
}
static void cpulist(const cpu_set_t *s, char *out, size_t m) {
  out[0] = 0; size_t o = 0;
  for (int i = 0; i < CPU_SETSIZE && o + 8 < m; i++) if (CPU_ISSET(i, s)) o += snprintf(out + o, m - o, "%s%d", o ? "," : "", i);
}
__attribute__((constructor)) static void pin_init(void) {
  const char *e = getenv("PIN_ENABLE");
  if (!e || strcmp(e, "1")) return;
  real_pc = (pc_fn)dlsym(RTLD_NEXT, "pthread_create");
  have_dump = parse_cpus(getenv("PIN_DUMP_CPUS"), &dump_set) > 0;
  have_prof = parse_cpus(getenv("PIN_PROFILER_CPUS"), &prof_set) > 0;
  if (getenv("PIN_DUMP_MATCH")) dump_match = getenv("PIN_DUMP_MATCH");
  const char *r = getenv("PIN_PROFILER_RANGES");
  if (r && *r) { char *c = strdup(r), *sv, *t;
    for (t = strtok_r(c, ",", &sv); t && nranges < MAXR; t = strtok_r(NULL, ",", &sv)) {
      char m[128]; unsigned long a, b;
      if (sscanf(t, "%127[^:]:%lx:%lx", m, &a, &b) == 3) { strcpy(ranges[nranges].mod, m); ranges[nranges].start = a; ranges[nranges].size = b; nranges++; } }
    free(c); }
  const char *d = getenv("PIN_LOG_DIR");
  if (d) { char h[64], p[512]; gethostname(h, sizeof h);
    snprintf(p, sizeof p, "%s/pin_%s_%d.log", d, h, (int)getpid());
    logfd = open(p, O_WRONLY | O_CREAT | O_APPEND, 0644); }
  void *bt[4]; backtrace(bt, 4); /* force libgcc load now, not inside pthread_create */
  enabled = 1;
  char a[256], b[256]; cpulist(&dump_set, a, sizeof a); cpulist(&prof_set, b, sizeof b);
  rec("INIT pid=%d dump_cpus=[%s] profiler_cpus=[%s] profiler_ranges=%d dump_match=%s\n", (int)getpid(), a, b, nranges, dump_match);
}
struct tramp { void *(*fn)(void *); void *arg; int role; };
static void *trampoline(void *p) {
  struct tramp t = *(struct tramp *)p; free(p);
  const cpu_set_t *want = t.role == 1 ? &dump_set : &prof_set;
  const char *name = t.role == 1 ? "dump" : "profiler";
  int rc = sched_setaffinity(0, sizeof(cpu_set_t), want);
  cpu_set_t got; CPU_ZERO(&got); sched_getaffinity(0, sizeof got, &got);
  char w[256], g[256]; cpulist(want, w, sizeof w); cpulist(&got, g, sizeof g);
  struct timespec ts; clock_gettime(CLOCK_REALTIME, &ts);
  rec("THREAD role=%s pid=%d tid=%ld t=%ld.%06ld setaffinity_rc=%d wanted=[%s] readback=[%s] match=%d\n", name, (int)getpid(),
      (long)syscall(SYS_gettid), (long)ts.tv_sec, ts.tv_nsec / 1000, rc, w, g, CPU_EQUAL(want, &got));
  return t.fn(t.arg);
}
static int in_profiler_range(void) {
  if (!nranges) return 0;
  void *bt[12]; int n = backtrace(bt, 12);
  for (int i = 1; i < n; i++) { Dl_info di; if (!dladdr(bt[i], &di) || !di.dli_fname) continue;
    unsigned long off = (unsigned long)bt[i] - (unsigned long)di.dli_fbase;
    for (int r = 0; r < nranges; r++)
      if (strstr(di.dli_fname, ranges[r].mod) && off >= ranges[r].start && off < ranges[r].start + ranges[r].size) return 1; }
  return 0;
}
int pthread_create(pthread_t *th, const pthread_attr_t *attr, void *(*fn)(void *), void *arg) {
  if (!real_pc) real_pc = (pc_fn)dlsym(RTLD_NEXT, "pthread_create");
  if (enabled && getenv("PIN_TRACE")) { /* debug: log call sites of every pthread_create */
    void *bt[8]; int n = backtrace(bt, 8);
    for (int i = 1; i < n && i < 5; i++) { Dl_info d; if (dladdr(bt[i], &d) && d.dli_fname) {
      const char *b = strrchr(d.dli_fname, '/'); b = b ? b + 1 : d.dli_fname;
      if (strstr(b, "nccl") || strstr(b, "inspector")) rec("TRACE frame=%d %s+0x%lx sym=%s\n", i, b, (unsigned long)bt[i] - (unsigned long)d.dli_fbase, d.dli_sname ? d.dli_sname : "?"); } }
  }
  if (enabled) {
    int role = 0; Dl_info di;
    if (have_dump && dladdr((void *)fn, &di) && di.dli_fname && strstr(di.dli_fname, dump_match)) role = 1;
    else if (have_prof && in_profiler_range()) role = 2;
    if (role) { struct tramp *t = malloc(sizeof *t);
      if (t) { t->fn = fn; t->arg = arg; t->role = role; return real_pc(th, attr, trampoline, t); } }
  }
  return real_pc(th, attr, fn, arg);
}
