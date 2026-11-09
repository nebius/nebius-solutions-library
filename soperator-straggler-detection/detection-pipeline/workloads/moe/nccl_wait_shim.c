/* nccl_wait_shim.c -- Option E (2026-11): LD_PRELOAD shim, CUDA-event-based
 * real GPU-timeline timing for ncclSend/ncclRecv. Design: DESIGN_NOTES
 * §6.36.4. Built specifically to close the gap Option D (model_moe.py's own
 * Python-level timing, §6.35) and arrival_order.py (§6.36.2) each left open
 * for a SINGLE purpose: a zero-customer-code-change signal that measures
 * real GPU-side enqueue-order timing, not CPU-dispatch time (Option D's
 * flaw, confirmed: a host call to ncclSend/ncclRecv enqueues onto a CUDA
 * stream and returns almost immediately -- nccl.h's own ncclGroupEnd()
 * documentation says explicitly "only guarantees the operations are
 * enqueued on the streams, not that the operation is effectively done")
 * and not a message-size-heuristic round boundary (arrival_order.py's
 * flaw, confirmed in §6.36.2).
 *
 * Does NOT modify NCCL or Inspector's own source. Interposes via
 * LD_PRELOAD exactly like this project's own qp_rate_limit_shim.c already
 * does for ibv_modify_qp -- real, published precedent for this exact
 * technique on ncclSend/ncclRecv specifically also exists outside this
 * project (timemory's GOTCHA wrappers, NVIDIA's own "NCCL Checkpoint
 * Shim"; both cited in DESIGN_NOTES §6.36.3).
 *
 * Zero customer-code-change, by construction: loaded via LD_PRELOAD at the
 * launcher/environment level (the same --export=ALL,LD_PRELOAD=... pattern
 * rank_fault_wrapper.sh already uses for qp_rate_limit_shim.so), never
 * touching model_moe.py or any customer model file. Confirmed technically
 * viable in this project's own actual training container before this file
 * was written (DESIGN_NOTES §6.36.3: `nm -D` on libtorch_cuda.so shows
 * ncclSend as an undefined/dynamically-linked symbol, `ldd` confirms a
 * real dynamic dependency on libnccl.so.2 -- not statically linked).
 *
 * No explicit -lnccl/-lcudart link dependency: every real NCCL/CUDA symbol
 * this shim calls (ncclSend, ncclRecv, ncclCommUserRank, ncclCommCount, and
 * all six cudaEvent* functions) is resolved explicitly at runtime via
 * dlsym(), never left as a plain extern reference. This was NOT the
 * original design -- inspector-plugin/Makefile's own precedent (plain
 * extern cudaGetDevice(), relying on load-time resolution against whatever
 * CUDA runtime the training process already has loaded) was tried first
 * and found, live, to NOT generalize safely to this shim's own loading
 * context: a plain RTLD_NEXT dlsym() for ncclSend/ncclRecv returned NULL
 * (a null-pointer call through it produced a real, reproducible SIGSEGV on
 * every rank), and plain extern references to ncclCommUserRank/
 * ncclCommCount produced a separate real "undefined symbol" load-time
 * failure (exit code 127, every rank). Both were root-caused and fixed by
 * explicit dlsym(RTLD_NEXT, ...) with an RTLD_NOLOAD dlopen() fallback --
 * the same real, already-documented pattern qp_rate_limit_shim.c uses for
 * ibv_modify_qp. Applied proactively to the CUDA event functions too,
 * rather than assuming they would fare better after two different symbols
 * already proved the "plain extern, load-time resolution" assumption wrong
 * in this exact shim.
 *
 * Output: one new, separate JSON-line dump-file channel per rank,
 * apptiming-{hostname}-pid{pid}.jsonl under $NCCL_INSPECTOR_DUMP_DIR --
 * the SAME file name/location convention Option D's _emit_apptiming()
 * already established (model_moe.py), specifically so the already-built
 * poll_apptiming_files()/_flush_apptiming_window()/_check_alltoall_app_
 * timing() ingestion chain (aggregator/node_aggregator_ref.py,
 * alerting/alert_engine.py) can be reused for this new signal too, with
 * only the record schema's own "call" field distinguishing the two
 * sources ("send"/"recv" here vs. "dispatch"/"combine" from Option D).
 *
 * Sampling-safe by construction (Step 3's own "sample rather than
 * instrument every call" interest, DESIGN_NOTES §6.36.3): a small, FIXED
 * pool of CUDA events (POOL_SIZE) is reused round-robin. If a pool slot's
 * previous event has not yet completed when its turn comes around again
 * (checked via a non-blocking cudaEventQuery, never cudaEventSynchronize
 * in the hot path), that one sample is simply dropped -- no unbounded
 * growth, no blocking, no synchronous GPU wait ever introduced into the
 * real training critical path. This directly reuses this project's own
 * "no unbounded accumulator" lesson (node_aggregator_ref.py's own
 * `all_vals` memory-growth bug, cited in docs/straggler_dectection_
 * history.md Phase 8/14).
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/time.h>
#include <pthread.h>
#include <nccl.h>
#include <cuda_runtime.h>

#define POOL_SIZE 64

typedef struct {
    int in_flight;          /* 1 once recorded, 0 once drained/dropped */
    int has_event;           /* 1 once this slot's cudaEvent_t was successfully
                                 created at process startup -- allocated exactly
                                 once per process, never per-call (see lazy_init's
                                 own "real, measured overhead fix" comment). */
    int peer;
    char direction;          /* 'S' (send) or 'R' (recv) */
    cudaEvent_t event;
    int rank;                /* real, this-process NCCL rank, from ncclCommUserRank()
                                 at record time -- written into the JSON record so
                                 node_aggregator_ref.py's existing poll_apptiming_files()
                                 (built for Option D's model_moe.py schema, §6.35.3) can
                                 ingest this channel's records unchanged: it reads
                                 d["rank"]/d.get("world_size") directly from each line,
                                 not from any other source. */
    int world_size;
    long long round_hint;    /* best-effort: a monotonic per-rank call counter,
                                 NOT a cross-rank-aligned "round" (that's exactly
                                 the concept that broke arrival_order.py, §6.36.2
                                 -- deliberately not relied on here for anything
                                 beyond human-readable ordering in the dump; real
                                 cross-rank comparison is done entirely via
                                 abs_ts_us in alert_engine.py, not via this field). */
} pool_slot_t;

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static pool_slot_t g_pool[POOL_SIZE];
static int g_pool_next = 0;
static long long g_call_counter = 0;

static int g_initialized = 0;
static int g_anchor_ready = 0;
static cudaEvent_t g_anchor_event;
static long long g_anchor_host_us = 0;

static FILE *g_dump_fh = NULL;
static char g_hostname[256] = "unknown";
static long g_pid = 0;

static ncclResult_t (*real_ncclSend)(const void*, size_t, ncclDataType_t, int, ncclComm_t, cudaStream_t) = NULL;
static ncclResult_t (*real_ncclRecv)(void*, size_t, ncclDataType_t, int, ncclComm_t, cudaStream_t) = NULL;
/* Live-confirmed necessary (not theoretical, same class of bug as ncclSend/
 * ncclRecv below): calling ncclCommUserRank/ncclCommCount as ordinary
 * extern references (resolved by the normal load-time dynamic-linker
 * search) produced a real, reproducible "undefined symbol:
 * ncclCommUserRank" failure at process start, exit code 127, on every
 * rank -- these must go through the same explicit dlsym()+RTLD_NOLOAD
 * resolution as ncclSend/ncclRecv, not plain extern calls. */
static ncclResult_t (*real_ncclCommUserRank)(const ncclComm_t, int*) = NULL;
static ncclResult_t (*real_ncclCommCount)(const ncclComm_t, int*) = NULL;

/* Live-confirmed necessary (not theoretical): RTLD_NEXT fails to resolve
 * ncclSend/ncclRecv/ncclCommUserRank/ncclCommCount in this project's actual
 * training container (RTLD_NEXT returns NULL for ncclSend/ncclRecv -- a
 * bare call through a NULL function pointer is exactly what produced a
 * real, reproducible SIGSEGV on every rank before this fallback was added;
 * plain extern references to ncclCommUserRank/ncclCommCount produced a
 * separate, also-real "undefined symbol" load-time failure, exit code 127,
 * before THEY were routed through this same function). Same real, already-
 * documented gotcha qp_rate_limit_shim.c hit for ibv_modify_qp ("the real
 * symbol isn't visible in the global scope chain after this shim") -- same
 * fix: find an already-loaded handle for the real library directly instead
 * of relying on scope ordering. libnccl.so.2 is already loaded into every
 * training process by the time any of these calls happen (PyTorch's own
 * ProcessGroupNCCL dlopens it during init_process_group()), so RTLD_NOLOAD
 * (never load a NEW copy, only look up an existing one) is safe and
 * correct here. */
static void *resolve_real_nccl_symbol(const char *name) {
    void *sym = dlsym(RTLD_NEXT, name);
    if (sym != NULL) return sym;
    void *h = dlopen("libnccl.so.2", RTLD_NOW | RTLD_NOLOAD);
    if (h != NULL) {
        sym = dlsym(h, name);
    }
    return sym; /* may still be NULL; callers must check before calling through */
}

/* Same real, live-confirmed-necessary pattern applied proactively to every
 * CUDA runtime call this shim makes, rather than waiting for a second
 * separate crash to prove it's needed here too: inspector-plugin/Makefile's
 * own precedent (plain extern cudaGetDevice(), "resolved at LOAD time by
 * whatever CUDA runtime the training process itself already has loaded")
 * does NOT hold for every symbol in every LD_PRELOAD loading context --
 * ncclCommUserRank/ncclCommCount already proved that assumption wrong once
 * for NCCL symbols in this exact shim; there is no reason to trust it
 * blindly for CUDA symbols instead of verifying. libcudart.so.12 confirmed
 * present and already loaded in this project's actual training container
 * (`find` against the nvcr.io/nvidia/pytorch:25.01-py3 image, DESIGN_NOTES
 * §6.36 Option E build notes). */
static void *resolve_real_cuda_symbol(const char *name) {
    void *sym = dlsym(RTLD_NEXT, name);
    if (sym != NULL) return sym;
    void *h = dlopen("libcudart.so.12", RTLD_NOW | RTLD_NOLOAD);
    if (h != NULL) {
        sym = dlsym(h, name);
    }
    return sym;
}

typedef cudaError_t (*cudaEventCreate_t)(cudaEvent_t*);
typedef cudaError_t (*cudaEventRecord_t)(cudaEvent_t, cudaStream_t);
typedef cudaError_t (*cudaEventQuery_t)(cudaEvent_t);
typedef cudaError_t (*cudaEventElapsedTime_t)(float*, cudaEvent_t, cudaEvent_t);
typedef cudaError_t (*cudaEventSynchronize_t)(cudaEvent_t);
typedef cudaError_t (*cudaEventDestroy_t)(cudaEvent_t);

static cudaEventCreate_t real_cudaEventCreate = NULL;
static cudaEventRecord_t real_cudaEventRecord = NULL;
static cudaEventQuery_t real_cudaEventQuery = NULL;
static cudaEventElapsedTime_t real_cudaEventElapsedTime = NULL;
static cudaEventSynchronize_t real_cudaEventSynchronize = NULL;
static cudaEventDestroy_t real_cudaEventDestroy = NULL;
static int g_cuda_resolved = 0;

/* All six resolved together, once, the first time any of them is needed --
 * simpler than six independent lazy-init checks, and the cost (a handful of
 * dlsym calls) only happens once per process. If ANY fails to resolve,
 * g_cuda_resolved stays 0 and every CUDA-event call site below treats that
 * as "CUDA timing unavailable this process" and skips cleanly rather than
 * risking a null-pointer call. */
static void resolve_cuda_symbols(void) {
    if (g_cuda_resolved) return;
    real_cudaEventCreate = (cudaEventCreate_t)resolve_real_cuda_symbol("cudaEventCreate");
    real_cudaEventRecord = (cudaEventRecord_t)resolve_real_cuda_symbol("cudaEventRecord");
    real_cudaEventQuery = (cudaEventQuery_t)resolve_real_cuda_symbol("cudaEventQuery");
    real_cudaEventElapsedTime = (cudaEventElapsedTime_t)resolve_real_cuda_symbol("cudaEventElapsedTime");
    real_cudaEventSynchronize = (cudaEventSynchronize_t)resolve_real_cuda_symbol("cudaEventSynchronize");
    real_cudaEventDestroy = (cudaEventDestroy_t)resolve_real_cuda_symbol("cudaEventDestroy");
    if (real_cudaEventCreate && real_cudaEventRecord && real_cudaEventQuery &&
        real_cudaEventElapsedTime && real_cudaEventSynchronize && real_cudaEventDestroy) {
        g_cuda_resolved = 1;
    }
}

static long long now_us(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (long long)tv.tv_sec * 1000000LL + (long long)tv.tv_usec;
}

/* Best-effort, never fatal: a failure anywhere in here must never take
 * down training -- this is a diagnostic shim, not a dependency. */
static void lazy_init(cudaStream_t stream) {
    if (g_initialized) return;
    g_initialized = 1;

    gethostname(g_hostname, sizeof(g_hostname) - 1);
    g_pid = (long)getpid();

    const char *dump_dir = getenv("NCCL_INSPECTOR_DUMP_DIR");
    const char *enabled = getenv("NCCL_WAIT_SHIM_ENABLED");
    if (enabled != NULL && (strcmp(enabled, "0") == 0 || strcmp(enabled, "false") == 0)) {
        return; /* g_dump_fh stays NULL -- every call becomes a cheap no-op below */
    }
    if (dump_dir != NULL && dump_dir[0] != '\0') {
        char path[1024];
        snprintf(path, sizeof(path), "%s/apptiming-%s-pid%ld.jsonl", dump_dir, g_hostname, g_pid);
        g_dump_fh = fopen(path, "a");
        if (g_dump_fh != NULL) {
            setvbuf(g_dump_fh, NULL, _IOLBF, 0); /* line-buffered, same durability profile as Option D's Python writer */
        }
    }

    for (int i = 0; i < POOL_SIZE; i++) {
        g_pool[i].in_flight = 0;
        g_pool[i].has_event = 0;
    }

    resolve_cuda_symbols();
    if (!g_cuda_resolved) return; /* CUDA timing unavailable this process -- no anchor,
                                      every later call site's own g_anchor_ready/g_cuda_resolved
                                      check turns itself into a clean no-op. */

    /* One-time anchor: a single real GPU/host clock correlation point,
     * established once per process via one deliberate, one-time
     * cudaEventSynchronize() (the ONLY blocking CUDA call this shim ever
     * makes) -- every subsequent per-call event is correlated against
     * this anchor via cudaEventElapsedTime(), never via another sync. */
    if (real_cudaEventCreate(&g_anchor_event) == cudaSuccess) {
        if (real_cudaEventRecord(g_anchor_event, stream) == cudaSuccess) {
            if (real_cudaEventSynchronize(g_anchor_event) == cudaSuccess) {
                g_anchor_host_us = now_us();
                g_anchor_ready = 1;
            }
        }
    }

    /* Real, measured overhead fix (2026-11): the first working version of
     * this shim called cudaEventCreate()/cudaEventDestroy() on EVERY single
     * intercepted call -- live-measured at ~200x slowdown (90-130ms/round
     * vs. a ~0.5ms shim-free baseline) on a healthy 10-step run, because
     * cudaEventCreate/cudaEventDestroy are expensive driver allocation
     * calls, never meant to be issued per-call at this volume. Fixed by
     * allocating every pool slot's cudaEvent_t exactly ONCE here, then
     * reusing the SAME event object via cudaEventRecord() on every later
     * call (cudaEventRecord on an already-created event is a cheap,
     * designed-for-reuse operation -- it just re-arms the existing event,
     * no new allocation) -- never created or destroyed again for the
     * lifetime of the process. g_pool[i].event is now valid for the whole
     * process once this returns successfully; has_event tracks whether
     * that allocation itself succeeded, separately from in_flight (whether
     * the slot currently holds an undrained sample). */
    int all_events_ok = 1;
    for (int i = 0; i < POOL_SIZE; i++) {
        if (real_cudaEventCreate(&g_pool[i].event) != cudaSuccess) {
            all_events_ok = 0;
            break;
        }
        g_pool[i].has_event = 1;
    }
    if (!all_events_ok) {
        g_cuda_resolved = 0; /* can't safely use a partially-allocated pool -- disable
                                 cleanly rather than risk calling cudaEventRecord on an
                                 event that was never actually created. */
    }
}

/* Drains one pool slot if its event has genuinely completed (non-blocking
 * cudaEventQuery only -- never cudaEventSynchronize here). Writes one real
 * JSON-line record with an absolute, cross-rank-comparable microsecond
 * timestamp (g_anchor_host_us + elapsed-since-anchor), the same absolute-
 * timestamp convention Inspector's own event_trace_ts already uses, so
 * alert_engine.py's existing cross-member comparisons apply unchanged. */
static void try_drain(int idx) {
    pool_slot_t *s = &g_pool[idx];
    if (!s->in_flight) return;
    cudaError_t q = real_cudaEventQuery(s->event);
    if (q == cudaErrorNotReady) {
        return; /* still in flight -- leave it, check again next time this slot comes around */
    }
    if (q == cudaSuccess && g_anchor_ready && g_dump_fh != NULL) {
        float ms = 0.0f;
        if (real_cudaEventElapsedTime(&ms, g_anchor_event, s->event) == cudaSuccess) {
            long long abs_ts_us = g_anchor_host_us + (long long)(ms * 1000.0f);
            fprintf(g_dump_fh,
                    "{\"ts_us\": %lld, \"hostname\": \"%s\", \"pid\": %ld, \"rank\": %d, "
                    "\"world_size\": %d, \"call\": \"%s\", \"peer\": %d, \"round\": %lld}\n",
                    abs_ts_us, g_hostname, g_pid, s->rank, s->world_size,
                    s->direction == 'S' ? "send" : "recv", s->peer, s->round_hint);
        }
    }
    /* cudaErrorNotReady is the only case we don't clear the query's own
     * sticky error state for -- cudaEventQuery's own contract says a
     * second call is safe and expected until the event completes. Any
     * other error (including a genuine failure) is silently dropped: a
     * missed diagnostic sample is acceptable, a crashed training process
     * is not. The event object itself is NEVER destroyed here -- it is
     * reused for this slot's entire process lifetime (see lazy_init); only
     * the in_flight flag resets, freeing the slot for its next cudaEventRecord(). */
    s->in_flight = 0;
}

/* Real, measured overhead finding (2026-11): reusing pre-allocated CUDA
 * events (the fix above this comment in lazy_init/try_drain/record_call)
 * did NOT meaningfully change wall-clock time (live-measured: 16:32 before
 * vs. 16:40 after, both for the same 10-step job) -- the dominant cost is
 * sheer CALL VOLUME, not the create/destroy pattern: a 10-step run issues
 * ~700,000+ Send/Recv calls per rank, and this shim was instrumenting
 * every single one, including a real disk write (fprintf to a line-
 * buffered file -- a real write() syscall per line) for most of them,
 * since most individual Send/Recv events complete fast enough to be
 * drained the very next time their pool slot comes back around. Fixed by
 * sampling explicitly: only 1 call in every NCCL_WAIT_SHIM_SAMPLE_RATE
 * (default 50) is ever instrumented at all -- every other call returns
 * immediately after a single cheap integer increment/modulo, before ANY
 * CUDA or I/O work. This directly reuses this project's own established
 * "sample rather than instrument every call" principle (Falcon/NDTimeline,
 * DESIGN_NOTES §6.36.3) rather than a create/destroy micro-optimization
 * that measurably did not address the real cost. Even at 1-in-50, a
 * 10-step run still yields on the order of 10,000+ real samples per rank
 * -- far more than the 30-sample detection window (§6.35's
 * APPTIMING_WINDOW_SIZE) needs. */
static long long g_call_tick = 0;
static int g_sample_rate = 0;

static void record_call(ncclComm_t comm, int peer, char direction, cudaStream_t stream) {
    if (g_sample_rate == 0) {
        const char *sr = getenv("NCCL_WAIT_SHIM_SAMPLE_RATE");
        g_sample_rate = (sr != NULL && atoi(sr) > 0) ? atoi(sr) : 50;
    }
    g_call_tick++;
    if (g_call_tick % g_sample_rate != 0) return; /* the cheap path: skip everything below */

    lazy_init(stream);
    if (g_dump_fh == NULL || !g_cuda_resolved) return; /* shim disabled, dump dir not set,
                                                            or CUDA symbols unavailable --
                                                            cheap no-op either way */

    /* Resolved and queried once per process, not once per call: rank/
     * world_size don't change across calls for this workload's one,
     * persistent default communicator -- caching this (same simplification
     * model_moe.py's own MOE_RANK/MOE_WORLD_SIZE globals already make) cuts
     * two extra NCCL API round-trips off every single intercepted call,
     * on top of the cudaEventCreate/Destroy fix above. */
    static int rank_cached = 0, g_cached_rank = -1, g_cached_world_size = -1;
    if (!rank_cached) {
        if (!real_ncclCommUserRank) real_ncclCommUserRank = (ncclResult_t (*)(const ncclComm_t, int*))
                                                             resolve_real_nccl_symbol("ncclCommUserRank");
        if (!real_ncclCommCount) real_ncclCommCount = (ncclResult_t (*)(const ncclComm_t, int*))
                                                       resolve_real_nccl_symbol("ncclCommCount");
        if (real_ncclCommUserRank) (void)real_ncclCommUserRank(comm, &g_cached_rank);
        if (real_ncclCommCount) (void)real_ncclCommCount(comm, &g_cached_world_size);
        rank_cached = 1;
    }

    pthread_mutex_lock(&g_lock);
    int idx = g_pool_next;
    g_pool_next = (g_pool_next + 1) % POOL_SIZE;
    long long call_no = ++g_call_counter;
    pthread_mutex_unlock(&g_lock);

    try_drain(idx); /* drain whatever was in this slot before reusing it --
                        this IS the sampling mechanism: a slot still in flight
                        when its turn comes back around is dropped, not waited
                        on or grown into a second buffer. */

    pool_slot_t *s = &g_pool[idx];
    if (!s->has_event) return; /* pool allocation failed at startup -- clean no-op,
                                    never falls back to a per-call cudaEventCreate. */
    if (real_cudaEventRecord(s->event, stream) != cudaSuccess) {
        return; /* leave in_flight as whatever it was; the event object itself
                    is still valid and reused next time this slot comes around. */
    }
    s->peer = peer;
    s->direction = direction;
    s->rank = g_cached_rank;
    s->world_size = g_cached_world_size;
    s->round_hint = call_no;
    s->in_flight = 1;
}

ncclResult_t ncclSend(const void* sendbuff, size_t count, ncclDataType_t datatype,
                       int peer, ncclComm_t comm, cudaStream_t stream) {
    if (!real_ncclSend) real_ncclSend = (ncclResult_t (*)(const void*, size_t, ncclDataType_t, int, ncclComm_t, cudaStream_t))
                                         resolve_real_nccl_symbol("ncclSend");
    if (!real_ncclSend) return ncclInternalError; /* never crash: an unresolvable
                                                       real symbol must fail loudly
                                                       at the NCCL-result level, not
                                                       via a null-pointer call. */
    ncclResult_t r = real_ncclSend(sendbuff, count, datatype, peer, comm, stream);
    if (r == ncclSuccess) record_call(comm, peer, 'S', stream);
    return r;
}

ncclResult_t ncclRecv(void* recvbuff, size_t count, ncclDataType_t datatype,
                       int peer, ncclComm_t comm, cudaStream_t stream) {
    if (!real_ncclRecv) real_ncclRecv = (ncclResult_t (*)(void*, size_t, ncclDataType_t, int, ncclComm_t, cudaStream_t))
                                         resolve_real_nccl_symbol("ncclRecv");
    if (!real_ncclRecv) return ncclInternalError;
    ncclResult_t r = real_ncclRecv(recvbuff, count, datatype, peer, comm, stream);
    if (r == ncclSuccess) record_call(comm, peer, 'R', stream);
    return r;
}
