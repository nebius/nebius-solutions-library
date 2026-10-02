#pragma once

#include <pthread.h>

#include "json.h"
#include "common.h"
#include "version.h"

#define MAX_CHANNELS                     64

// Ported from a prior, separately-documented development track's
// validated design (not a commit in this repo's own history -- verified
// directly, this branch's inspector-plugin/ source had never received
// it): replaces the original single-slot latch (one completedCollInfo
// + one dirty flag per communicator), which silently overwrote/dropped
// a completed collective's record whenever a second one completed
// before the dump thread's next wakeup -- confirmed live this session,
// ~17-20% record loss on high-frequency communicators (TP4 AllReduce)
// at the default 500us dump-thread interval. 256 matches the prior
// validated capacity; re-confirmed appropriate here via direct
// sizeof(inspectorCompletedCollInfo) measurement against THIS branch's
// own struct layout (3168 bytes/record -> exactly 792.0 KiB/comm/rank
// for 256 slots, same figure the prior validation reported -- no
// adjustment needed). Must be a power of 2 so index wrapping is a cheap
// bitmask (& (CAPACITY-1)) instead of a modulo.
#define INSPECTOR_RING_CAPACITY          256

// P32's own deferred-free retirement queue (see inspector_plugin.cc's
// own full docstring on inspectorRetireCollInfo for the real
// use-after-free bug this closes), scoped from one process-wide queue
// to one per communicator -- confirmed the dominant per-collective
// hot-path cost this session (~1800-3060 cycles/finalize event, 2.5-3x
// the next-largest phase) via direct, live instrumentation, traced to
// real cross-communicator contention on a single global
// pthread_mutex_t. Verified safe before narrowing, not assumed:
// re-read P32's own docstring, which explicitly calibrates
// RETIRE_QUEUE_CAPACITY's depth to real ELAPSED TIME ("several real
// seconds of buffering... at DLRM's steady-state call rate"), not to a
// cross-communicator object count -- scoping per-communicator can only
// ever INCREASE this real-time margin for any given communicator's own
// objects (no longer sharing queue depth with other communicators'
// retirements, which drained it faster in the old global design),
// never decrease it. 1000 is unchanged from the original, already-
// validated depth.
#define RETIRE_QUEUE_CAPACITY 1000

#define INS_CHK_GOTO(call, res, label)                                  \
  do {                                                                  \
    res = call;                                                         \
    if (inspectorSuccess != res) {                                      \
      INFO(NCCL_INSPECTOR, "%s:%d -> error %d: %s", __FILE__, __LINE__, res, \
           inspectorErrorString(res));                                  \
      goto label;                                                       \
    }                                                                   \
  } while (0);


typedef enum {
  ncclFuncBroadcast = 0,
  ncclFuncReduce = 1,
  ncclFuncAllGather = 2,
  ncclFuncReduceScatter = 3,
  ncclFuncAllReduce = 4,
  ncclFuncSendRecv = 5,
  ncclFuncSend = 6,
  ncclFuncRecv = 7,
  ncclNumFuncs = 8
} ncclFunc_t;

typedef enum {
  inspectorSuccess = 0,
  inspectorUninitializedError,
  inspectorMemoryError,
  inspectorFileOpenError,
  inspectorDisabledError,
  inspectorLockError,
  inspectorPthreadError,
  inspectorJsonError,
  inspectorCudaError,
  inspectorBadHash,
  inspectorDeleteUnknownCommError,
  inspectorAddDuplicateCommError,
  inspectorNop,
  inspectorNullTally,
  inspectorGlobalInitError,
  inspectorReturn,
} inspectorResult_t;

typedef enum {
  inspectorTimingSourceKernelGpu = 0,
  inspectorTimingSourceKernelCpu = 1,
  inspectorTimingSourceCollectiveCpu = 2,
} inspectorTimingSource_t;

struct inspectorEventTraceInfo {
  uint64_t ts;
  uint64_t sn;
};

typedef enum {
  NCCL_INSP_EVT_TRK_COLL_START = 0,
  NCCL_INSP_EVT_TRK_COLL_STOP = 1,
  NCCL_INSP_EVT_TRK_COLL_NEVT = 2,
} inspectorEventTrkColl_t;

typedef enum {
  NCCL_INSP_EVT_TRK_KERNEL_START = 0,
  NCCL_INSP_EVT_TRK_KERNEL_STOP = 1,
  NCCL_INSP_EVT_TRK_KERNEL_RECORD = 2,
  NCCL_INSP_EVT_TRK_KERNEL_NEVT = 3,
} inspectorEventTrkKernel_t;

struct inspectorEventTrkKernelInfo {
  struct inspectorEventTraceInfo evntTrace[NCCL_INSP_EVT_TRK_KERNEL_NEVT];
};

struct inspectorEventTrkCollInfo {
  int sn;
  uint32_t nChannels;
  struct inspectorEventTraceInfo evntTrace[NCCL_INSP_EVT_TRK_COLL_NEVT];
  struct inspectorEventTrkKernelInfo kernelCh[MAX_CHANNELS];
};

struct inspectorCompletedCollInfo {
  ncclFunc_t func;
  uint64_t sn;
  size_t msgSizeBytes;
  uint64_t execTimeUsecs;
  inspectorTimingSource_t timingSource;
  double algoBwGbs;
  double busBwGbs;
  // Event trace information
  struct inspectorEventTrkCollInfo collEvtTrk;
};

enum {
  NCCL_COMM_HASH_LENGTH = 17
};

struct inspectorCollInfo; // forward decl -- inspectorCollInfo (below) is only ever referenced here via pointer, for the per-comm retirement queue

struct inspectorCommInfo {
  struct inspectorCommInfo* next;

  const char* commName;
  uint64_t commHash;
  char commHashStr[NCCL_COMM_HASH_LENGTH];
  int rank;
  int nranks;
  int nnodes;

  // Lock-free, bounded SPSC ring buffer (ring-buffer port -- see
  // INSPECTOR_RING_CAPACITY's own comment above for the real bug this
  // replaces). Single producer: this communicator's own NCCL proxy
  // progress thread (ncclProxyProgressCreate's guarded, one-time
  // pthread_create guarantees exactly one such thread per communicator
  // for any comm NOT created via an explicit ncclCommSplit-with-share
  // -- confirmed directly against this branch's real NCCL 2.28.9
  // source; Megatron/PyTorch's standard new_group()-based TP/PP/DP
  // group creation does not use that opt-in path, so this holds for
  // the workloads this project runs). Single consumer: the dump
  // thread. ringHead/ringTail are monotonically increasing (never
  // wrapped) produced/consumed counts -- the real array slot is always
  // index & (INSPECTOR_RING_CAPACITY-1). Overflow policy is
  // drop-newest: the producer never advances past a full buffer and
  // never touches ringTail, preserving the lock-free single-producer/
  // single-consumer invariant (an evict-oldest policy would require
  // the producer to also touch the consumer's own index, reintroducing
  // a race). All access is via __atomic_* builtins (matching NCCL's
  // own atomic-builtin idiom in this codebase) -- no mutex/rwlock
  // guards this structure at all, unlike the single-slot design it
  // replaces.
  struct inspectorCompletedCollInfo ringBuf[INSPECTOR_RING_CAPACITY];
  uint64_t ringHead;          // producer-owned; published with release so the consumer's acquire-load is guaranteed to see the fully-written slot
  uint64_t ringTail;          // consumer-owned; published with release once per drain so the producer's acquire-load sees freed capacity promptly
  uint64_t queueDropsTotal;   // cumulative; incremented atomically by the producer on every drop-newest event; surfaced in every dumped record (never silent) and in a rate-limited [WARN] (fires on drop counts 1, 2, 4, 8, 16, ... -- exact powers of two, never silent but never log-spamming either)

  // P32's deferred-free retirement queue (see inspector_plugin.cc's own
  // inspectorRetireCollInfo docstring for the real use-after-free bug
  // it closes), scoped per-communicator instead of one process-wide
  // queue -- see RETIRE_QUEUE_CAPACITY's own comment above for why this
  // is safe (can only increase the real-time retention margin, never
  // decrease it). retireLock guards all three fields below; explicitly
  // pthread_mutex_init'd in inspectorFillCommInfo and destroyed (along
  // with freeing any still-queued entries) in inspectorCommInfoListFinalize
  // when this communicator itself is torn down -- a real, new
  // correctness point this per-comm scoping introduces that the old
  // global queue never needed (a process-wide queue outlives every
  // individual communicator by construction; a per-comm one does not).
  struct inspectorCollInfo* retireQueue[RETIRE_QUEUE_CAPACITY];
  int retireHead;
  int retireCount;
  pthread_mutex_t retireLock;
};

struct inspectorKernelChInfo {
  uint64_t type;
  int refCount; /*unused*/
  struct inspectorCollInfo *collInfo;
  uint8_t channelId;
  uint64_t tsStartUsec;
  uint64_t tsCompletedUsec;
  uint64_t startGpuClk;
  uint64_t stopGpuClk;
};

struct inspectorCollInfo {
  uint64_t type;
  int refCount;
  struct inspectorCommInfo *commInfo;
  const char* func;
  uint64_t sn;
  size_t msgSizeBytes;
  uint64_t tsStartUsec;
  uint64_t tsCompletedUsec;
  uint32_t nChannels;
  uint32_t nKernelChStarted;
  uint32_t nKernelChCompleted;
  pthread_rwlock_t guard;
  struct inspectorKernelChInfo kernelCh[MAX_CHANNELS];
  struct inspectorEventTrkCollInfo collEvtTrk;
};



extern ncclDebugLogger_t logFn;
#define VERSION(...) logFn(NCCL_LOG_VERSION, NCCL_ALL, __FILE__, __LINE__, __VA_ARGS__)
#define INFO(FLAGS, ...) logFn(NCCL_LOG_INFO, (FLAGS), __func__, __LINE__, __VA_ARGS__)
#define WARN(...) logFn(NCCL_LOG_WARN, NCCL_ALL, __FILE__, __LINE__, __VA_ARGS__)

inline int ncclTypeSize(ncclDataType_t type) {
  switch (type) {
  case ncclInt8:
  case ncclUint8:
  case ncclFloat8e4m3:
  case ncclFloat8e5m2:
    return 1;
  case ncclFloat16:
  case ncclBfloat16:
    return 2;
  case ncclInt32:
  case ncclUint32:
  case ncclFloat32:
    return 4;
  case ncclInt64:
  case ncclUint64:
  case ncclFloat64:
    return 8;
  default:
    return -1;
  }
}

const char* inspectorErrorString(inspectorResult_t result);

inspectorResult_t inspectorLockInit(pthread_rwlock_t* lockRef);
inspectorResult_t inspectorLockDestroy(pthread_rwlock_t* lockRef);
inspectorResult_t inspectorLockRd(pthread_rwlock_t* lockRef);
inspectorResult_t inspectorLockWr(pthread_rwlock_t* lockRef);
inspectorResult_t inspectorUnlockRWLock(pthread_rwlock_t* lockRef);
inspectorResult_t inspectorGlobalInit(int rank);
inspectorResult_t inspectorGlobalFinalize();
uint64_t inspectorGetTime();
inspectorResult_t inspectorAddComm(struct inspectorCommInfo **commInfo,
                                   const char* commName, uint64_t commHash,
                                   int nNodes, int nranks, int rank);
inspectorResult_t inspectorDelComm(struct inspectorCommInfo *commInfo);

void inspectorUpdateCollPerf(struct inspectorCompletedCollInfo *completedColl,
                             struct inspectorCollInfo *collInfo);
ncclDataType_t inspectorStringToDatatype(const char* str);

void inspectorComputeCollBw(struct inspectorCommInfo *commInfo,
                            struct inspectorCompletedCollInfo *completedColl,
                            ncclFunc_t collType);
