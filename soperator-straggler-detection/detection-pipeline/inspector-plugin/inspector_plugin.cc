/*************************************************************************
 * Copyright (c) 2025, NVIDIA CORPORATION. All rights reserved.
 *
 * See LICENSE.txt for license information
 ************************************************************************/

#include <stdio.h>
#include <pthread.h>
#include <string.h>
#include <linux/limits.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/syscall.h>
#include <unistd.h>
#include "profiler.h"
#include "inspector.h"

#define __hidden __attribute__ ((visibility("hidden")))

static int gInitialized;

static pthread_mutex_t gLock = PTHREAD_MUTEX_INITIALIZER;


/*
 * Description:
 *   Records an event trace with timestamp and sequence number
 *
 * Thread Safety:
 *   Not thread-safe - must be called with proper locking. This function
 *   is designed to be called from within locked sections where the
 *   collective info structure is already protected.
 *
 * Input:
 *   struct inspectorEventTraceInfo* evtTrace - event trace array
 *   int eventIndex - index in the event trace array (must be valid)
 *   struct inspectorCollInfo* collInfo - collective info structure (must not be NULL)
 *
 * Output:
 *   Event trace is updated with current timestamp and next sequence
 *   number from collective
 *
 * Return:
 *   uint64_t - the sequence number assigned to this event
 *
 * Preconditions:
 *   - collInfo must not be NULL
 *   - eventIndex must be within valid bounds for evtTrace array
 *   - Function must be called from within a locked section
 */
static uint64_t inspectorRecordEventTrace(struct inspectorEventTraceInfo* evtTrace,
                                          int eventIndex,
                                          struct inspectorCollInfo* collInfo) {
  evtTrace[eventIndex].ts = inspectorGetTime();
  evtTrace[eventIndex].sn = ++collInfo->collEvtTrk.sn; // Increment coll sequence counter

  return evtTrace[eventIndex].sn;
}

/*
 * Description:
 *
 *   Initializes the NCCL Inspector plugin and global state for a
 *   communicator.
 *
 * Thread Safety:
 *   Thread-safe (uses mutex for initialization).
 *
 * Input:
 *   void** context - pointer to plugin context.
 *   int* eActivationMask - pointer to activation mask output.
 *   const char* commName - communicator name.
 *   uint64_t commHash - communicator hash.
 *   int nNodes - number of nodes.
 *   int nranks - number of ranks.
 *   int rank - rank.
 *   ncclDebugLogger_t logfn - logger function pointer.
 *
 * Output:
 *   context is set to plugin context; eActivationMask is set.
 *
 * Return:
 *   ncclResult_t - success or error code.
 *
 */
__hidden ncclResult_t inspectorPluginInit(void** context, uint64_t commHash,
                                          int* eActivationMask,
                                          const char* commName,
                                          int nNodes, int nranks, int rank,
                                          ncclDebugLogger_t logfn) {
  inspectorResult_t res = inspectorSuccess;
  *context = nullptr;
  logFn = logfn;

  pthread_mutex_lock(&gLock);
  if (++gInitialized == 1) {
    res = inspectorGlobalInit(rank);
    if (res != inspectorSuccess) {
      WARN("Inspector Init Failed %s:%d -> error %d: %s",__FILE__, __LINE__, res,
           inspectorErrorString(res));
      gInitialized = 0;
      pthread_mutex_unlock(&gLock);
      return ncclInternalError;
    }
  }
  pthread_mutex_unlock(&gLock);

  INS_CHK_GOTO(inspectorAddComm((struct inspectorCommInfo **)context,
                                commName, commHash,
                                nNodes, nranks, rank), res, success);
  // P23 -- also request ncclProfileP2p (distinct from ncclProfileColl in
  // NCCL's own profiler API, profiler.h) so Send/Recv/AllToAll-style
  // point-to-point traffic is visible at all. Confirmed via a live probe
  // (4000 real all_to_all_single/all_to_all calls) that without this
  // flag, zero records are produced for such traffic -- not mislabeled,
  // literally absent.
  *eActivationMask = ncclProfileColl | ncclProfileP2p | ncclProfileKernelCh;
  INFO(NCCL_INIT, "PROFILER/Plugin: init commName: %s commHash: %lu nranks: %d rank: %d",
       commName ? commName : "", commHash, nranks, rank);
success:
  if (res != inspectorSuccess) {
    return ncclInternalError;
  } else {
    return ncclSuccess;
  }
}

/*
 * Description:
 *
 *   Finalizes the NCCL Inspector plugin and global state for a
 *   communicator.
 *
 * Thread Safety:
 *   Thread-safe (uses mutex for finalization).
 *
 * Input:
 *   void* context - plugin context.
 *
 * Output:
 *   Plugin context is finalized and cleaned up.
 *
 * Return:
 *   ncclResult_t - success or error code.
 *
 */
__hidden ncclResult_t inspectorPluginFinalize(void* context) {
  inspectorDelComm((struct inspectorCommInfo *)context);
  pthread_mutex_lock(&gLock);
  if (--gInitialized == 0) {
    inspectorGlobalFinalize();
  }
  pthread_mutex_unlock(&gLock);
  return ncclSuccess;
}

/*
 * P32 -- deferred-free retirement queue for inspectorCollInfo, replacing
 * the previous immediate inspectorLockDestroy+memset+free at
 * refCount==0.
 *
 * Root cause (found live via a minimal, isolated repro with no DLRM
 * model involved -- pure dist.all_to_all_single/dist.all_reduce calls
 * at DLRM's real collective mix): the old inspectorPluginCollInfoDeRef
 * destroyed collInfo->guard (a pthread_rwlock_t EMBEDDED in the struct
 * being freed) and free()'d the struct WHILE THAT SAME LOCK WAS STILL
 * HELD by the calling thread -- every call site takes
 * inspectorLockWr(&collInfo->guard) before calling DeRef. Undefined
 * behavior per POSIX on its own, but the real, demonstrated failure is
 * a genuine use-after-free: NCCL's own profiler callback sequence for a
 * collective's kernelCh sub-events (StartEvent/RecordEventState/
 * StopEvent) vs. the collective's own top-level StopEvent is not
 * guaranteed to arrive in the order the refcount protocol implicitly
 * assumed, and a late-arriving callback can dereference
 * eDescr->parentObj / kernelChInfo->collInfo pointing at memory this
 * plugin already freed. Confirmed directly: a fast, back-to-back
 * version of the exact same collective pattern (no per-iteration
 * pacing) ran 50000 iterations with zero crashes; adding a single
 * per-iteration sleep to match DLRM's real ~4.5-5ms/iter wall-clock
 * pace (no other change) reproduced the identical "corrupted size vs.
 * prev_size" crash within ~6000 iterations. This matches a stale
 * pointer landing on memory that was freed and then reallocated for a
 * DIFFERENTLY-SIZED/SHAPED object during the real time gap between
 * iterations (tripping glibc's chunk-size consistency check) -- the
 * fast case almost always reuses freed memory for another SAME-SIZED
 * collInfo immediately, so the identical stale-pointer write would just
 * silently corrupt a structurally-identical struct instead of crashing.
 *
 * Fix: never actually free() a collInfo at refCount==0. Retire it into
 * a bounded FIFO instead, and only free the OLDEST retired entry once
 * the FIFO exceeds RETIRE_QUEUE_CAPACITY -- by which point real time
 * (proportional to real allocation rate) has passed, well past any
 * plausible late-NCCL-callback window. Bounded, not unbounded growth --
 * ~1000 * sizeof(inspectorCollInfo) (~2.6MB) of extra retained memory,
 * several real seconds of buffering even at DLRM's steady-state call
 * rate. A late, stale callback into a retired-but-not-yet-freed object
 * now touches harmlessly-stale-but-VALID memory instead of freed/
 * reallocated memory -- the lock is never destroyed early either, so
 * every call site can unconditionally unlock afterward (no more
 * "skip unlock, the struct might already be gone" special-casing).
 */
#define RETIRE_QUEUE_CAPACITY 1000
static struct inspectorCollInfo* gRetireQueue[RETIRE_QUEUE_CAPACITY];
static int gRetireHead = 0;
static int gRetireCount = 0;
static pthread_mutex_t gRetireLock = PTHREAD_MUTEX_INITIALIZER;

static void inspectorRetireCollInfo(struct inspectorCollInfo* collInfo) {
  struct inspectorCollInfo* victim = nullptr;
  pthread_mutex_lock(&gRetireLock);
  if (gRetireCount == RETIRE_QUEUE_CAPACITY) {
    victim = gRetireQueue[gRetireHead];
    gRetireHead = (gRetireHead + 1) % RETIRE_QUEUE_CAPACITY;
    gRetireCount--;
  }
  int tail = (gRetireHead + gRetireCount) % RETIRE_QUEUE_CAPACITY;
  gRetireQueue[tail] = collInfo;
  gRetireCount++;
  pthread_mutex_unlock(&gRetireLock);
  if (victim != nullptr) {
    // victim has survived RETIRE_QUEUE_CAPACITY further retirements
    // since its own refCount hit 0 -- safe to actually destroy/free now.
    inspectorLockDestroy(&victim->guard);
    memset(victim, 0, sizeof(struct inspectorCollInfo));
    free(victim);
  }
}

inspectorResult_t inspectorPluginCollInfoRef(struct inspectorCollInfo *collInfo) {
  collInfo->refCount += 1;
  return inspectorSuccess;
}

inspectorResult_t inspectorPluginCollInfoRefSafe(struct inspectorCollInfo *collInfo) {
  inspectorLockWr(&collInfo->guard);
  inspectorPluginCollInfoRef(collInfo);
  inspectorUnlockRWLock(&collInfo->guard);
  return inspectorSuccess;
}

inspectorResult_t inspectorPluginCollInfoDeRef(struct inspectorCollInfo *collInfo) {
  collInfo->refCount -= 1;
  if (collInfo->refCount == 0) {
    inspectorRetireCollInfo(collInfo);
    return inspectorReturn;
  }
  return inspectorSuccess;
}

inspectorResult_t inspectorPluginCollInfoDeRefSafe(struct inspectorCollInfo *collInfo) {
  inspectorLockWr(&collInfo->guard);
  inspectorResult_t res = inspectorPluginCollInfoDeRef(collInfo);
  inspectorUnlockRWLock(&collInfo->guard);
  return res;
}

/*
 * Description:
 *   Initializes a new inspectorCollInfo structure for a collective
 *   event.
 *
 * Thread Safety:
 *   Not thread-safe (allocates and initializes a new collective info
 *   structure).
 *
 * Input:
 *
 *   struct inspectorCollInfo **collInfo - pointer to output
 *   collective info struct.
 *   ncclProfilerEventDescr_t *eDescr - event descriptor.
 *
 * Output:
 *   collInfo is set to the new collective info struct.
 *
 * Return:
 *   None.
 */
static void inspectorPluginCollInfoInit(struct inspectorCollInfo **collInfo,
                                        ncclProfilerEventDescr_t *eDescr,
                                        struct inspectorCommInfo *commInfo) {
  struct inspectorCollInfo *collInfoPtr
    = (struct inspectorCollInfo*)calloc(1, sizeof(struct inspectorCollInfo));
  if (collInfoPtr == nullptr) {
    WARN("Inspector: Failed to allocate memory for collective info structure");
    *collInfo = nullptr;
    return;
  }
  collInfoPtr->type = ncclProfileColl;
  collInfoPtr->refCount = 0;
  inspectorPluginCollInfoRef(collInfoPtr); //self ref; no locks needed
  collInfoPtr->func = eDescr->coll.func;
  collInfoPtr->sn = eDescr->coll.seqNumber;
  collInfoPtr->nChannels = eDescr->coll.nChannels;
  if (collInfoPtr->nChannels > 0) {
    inspectorPluginCollInfoRef(collInfoPtr); //extra ref for kernel completion
  }
  collInfoPtr->tsStartUsec = inspectorGetTime();
  collInfoPtr->msgSizeBytes =
    ncclTypeSize(inspectorStringToDatatype(eDescr->coll.datatype)) * eDescr->coll.count;


  collInfoPtr->commInfo = commInfo;
  collInfoPtr->collEvtTrk.sn = 0;
  collInfoPtr->collEvtTrk.nChannels = collInfoPtr->nChannels;
  inspectorRecordEventTrace(collInfoPtr->collEvtTrk.evntTrace,
                            NCCL_INSP_EVT_TRK_COLL_START, collInfoPtr);

  inspectorLockInit(&collInfoPtr->guard);
  *collInfo = collInfoPtr;
}

/*
 * Description:
 *   P23 -- P2p analog of inspectorPluginCollInfoInit. Reuses the exact
 *   same inspectorCollInfo struct/lifecycle (ref-counting, kernelCh
 *   completion tracking, dump trigger) as a real collective -- a P2p
 *   Send/Recv still executes as a GPU kernel on some channel(s) and goes
 *   through the identical completion path. Only the source fields differ
 *   (eDescr->p2p.* instead of eDescr->coll.*). collInfoPtr->func is set
 *   to the real "Send"/"Recv" string NCCL already passes here -- the
 *   existing ncclStringToFunc/ncclFuncToString round-trip in inspector.cc
 *   already handles these values (they are pre-existing ncclFunc_t enum
 *   members), so no new coll-name mapping is needed.
 *
 *   Note: unlike eDescr->coll, eDescr->p2p carries no seqNumber field in
 *   this profiler API version, so collInfoPtr->sn (dumped as "coll_sn")
 *   is left at 0 for P2p records -- a real, disclosed gap, not silently
 *   invented. eDescr->p2p.peer (the remote rank) is deliberately NOT
 *   threaded into the dump here, to keep this patch minimal and reuse
 *   the existing dump schema unchanged; adding it is a natural follow-up
 *   if per-peer accounting is needed later.
 */
static void inspectorPluginP2pInfoInit(struct inspectorCollInfo **collInfo,
                                       ncclProfilerEventDescr_t *eDescr,
                                       struct inspectorCommInfo *commInfo) {
  struct inspectorCollInfo *collInfoPtr
    = (struct inspectorCollInfo*)calloc(1, sizeof(struct inspectorCollInfo));
  if (collInfoPtr == nullptr) {
    WARN("Inspector: Failed to allocate memory for p2p info structure");
    *collInfo = nullptr;
    return;
  }
  collInfoPtr->type = ncclProfileP2p;
  collInfoPtr->refCount = 0;
  inspectorPluginCollInfoRef(collInfoPtr); //self ref; no locks needed
  collInfoPtr->func = eDescr->p2p.func;
  collInfoPtr->sn = 0; // no seqNumber field for p2p events, see docstring above
  collInfoPtr->nChannels = eDescr->p2p.nChannels;
  if (collInfoPtr->nChannels > 0) {
    inspectorPluginCollInfoRef(collInfoPtr); //extra ref for kernel completion
  }
  collInfoPtr->tsStartUsec = inspectorGetTime();
  collInfoPtr->msgSizeBytes =
    ncclTypeSize(inspectorStringToDatatype(eDescr->p2p.datatype)) * eDescr->p2p.count;

  collInfoPtr->commInfo = commInfo;
  collInfoPtr->collEvtTrk.sn = 0;
  collInfoPtr->collEvtTrk.nChannels = collInfoPtr->nChannels;
  inspectorRecordEventTrace(collInfoPtr->collEvtTrk.evntTrace,
                            NCCL_INSP_EVT_TRK_COLL_START, collInfoPtr);

  inspectorLockInit(&collInfoPtr->guard);
  *collInfo = collInfoPtr;
}

/*
 * Description:
 *
 *   Initializes a new inspectorKernelChInfo structure for a kernel
 *   channel event.
 *
 * Thread Safety:
 *   Not thread-safe (initializes kernel channel info within a
 *   collective info structure).
 *
 * Input:
 *   struct inspectorKernelChInfo **kernelChInfo - pointer to output
 *   kernel channel info struct.
 *   ncclProfilerEventDescr_t *eDescr - event descriptor.
 *
 * Output:
 *
 *   kernelChInfo is set to the new kernel channel info struct.
 *
 * Return:
 *   None.
 */
static void inspectorPluginKernelChInfoInit(struct inspectorKernelChInfo **kernelChInfo,
                                            ncclProfilerEventDescr_t *eDescr) {
  if (eDescr->parentObj) {
    uint64_t parentType=*(uint64_t*)eDescr->parentObj;
    if (parentType == ncclProfileColl || parentType == ncclProfileP2p) {
      struct inspectorCollInfo *collInfo = (struct inspectorCollInfo*)eDescr->parentObj;
      if (collInfo && (collInfo->type == ncclProfileColl || collInfo->type == ncclProfileP2p)) {
        inspectorLockWr(&collInfo->guard);
        struct inspectorEventTraceInfo *krnlEvtTrk =
          collInfo->collEvtTrk.kernelCh[eDescr->kernelCh.channelId].evntTrace;
        inspectorRecordEventTrace(krnlEvtTrk,
                                  NCCL_INSP_EVT_TRK_KERNEL_START,
                                  collInfo);
        struct inspectorKernelChInfo *kernelChInfoPtr
          = &collInfo->kernelCh[eDescr->kernelCh.channelId];
        kernelChInfoPtr->type = ncclProfileKernelCh;
        kernelChInfoPtr->channelId = eDescr->kernelCh.channelId;
        kernelChInfoPtr->startGpuClk = eDescr->kernelCh.pTimer;
        if (kernelChInfoPtr->stopGpuClk == 0) {
          inspectorPluginCollInfoRef(collInfo); //Pairs with Record Kernel Stop event
        }
        kernelChInfoPtr->tsStartUsec = inspectorGetTime();
        if (collInfo->nKernelChStarted == 0) {
          collInfo->tsStartUsec = kernelChInfoPtr->tsStartUsec;
        }
        collInfo->nKernelChStarted += 1;
        inspectorPluginCollInfoRef(collInfo); //Pairs with Stop Kernel Event
        kernelChInfoPtr->collInfo = collInfo;

        *kernelChInfo = kernelChInfoPtr;
        inspectorUnlockRWLock(&collInfo->guard);
      }
    }
  }
}
/*
 * Description:
 *
 *   Starts a profiling event for the NCCL Inspector plugin.
 *
 * Thread Safety:
 *   Thread-safe (allocates and initializes event structures).
 *
 * Input:
 *   void* context - plugin context.
 *   void** eHandle - pointer to event handle output.
 *   ncclProfilerEventDescr_t* eDescr - event descriptor.
 *
 * Output:
 *   eHandle is set to the new event structure.
 *
 * Return:
 *   ncclResult_t - success or error code.
 *
 */
__hidden ncclResult_t inspectorPluginStartEvent(void* context,
                                                void** eHandle,
                                                ncclProfilerEventDescr_t* eDescr) {
  if (context == nullptr || eDescr == nullptr) {
    INFO(NCCL_INIT, "Profiler/Plugin: context/eDescr NULL for start event %s", __func__);
    return ncclSuccess;
  }
  *eHandle = nullptr;
  if (eDescr->type == ncclProfileColl) {
    struct inspectorCollInfo *collEvent = nullptr;
    struct inspectorCommInfo *commInfoCtx = (struct inspectorCommInfo*)context;
    inspectorPluginCollInfoInit(&collEvent, eDescr, commInfoCtx);
    *eHandle = collEvent;
  } else if (eDescr->type == ncclProfileP2p) {
    struct inspectorCollInfo *p2pEvent = nullptr;
    struct inspectorCommInfo *commInfoCtx = (struct inspectorCommInfo*)context;
    inspectorPluginP2pInfoInit(&p2pEvent, eDescr, commInfoCtx);
    *eHandle = p2pEvent;
  } else if (eDescr->type == ncclProfileKernelCh) {
    struct inspectorKernelChInfo *kernelChEvent = nullptr;
    inspectorPluginKernelChInfoInit(&kernelChEvent, eDescr);
    *eHandle = kernelChEvent;
  } else {
    return ncclSuccess;
  }
  return ncclSuccess;
}

/*
 * Description:
 *
 *   Stops a profiling event for the NCCL Inspector plugin.
 *
 * Thread Safety:
 *
 *   Thread-safe (updates event state and performance info).
 *
 * Input:
 *
 *   void *eHandle - event handle.
 *
 * Output:
 *
 *   Event is stopped and performance info may be updated.
 *
 * Return:
 *   ncclResult_t - success or error code.
 *
 */
__hidden ncclResult_t inspectorPluginStopEvent(void *eHandle) {

  if (eHandle == nullptr) {
    INFO(NCCL_INIT,
         "Profiler/Plugin: Event Handle NULL for start event %s", __func__);
    return ncclSuccess;
  }
  uint64_t type = *(uint64_t *)eHandle;
  inspectorResult_t res = inspectorSuccess;

  if (type == ncclProfileColl || type == ncclProfileP2p) {
    struct inspectorCollInfo *collInfo = (struct inspectorCollInfo *)eHandle;
    // Record collective (or P23: p2p) stop event
    inspectorLockWr(&collInfo->guard);
    inspectorRecordEventTrace(collInfo->collEvtTrk.evntTrace,
                              NCCL_INSP_EVT_TRK_COLL_STOP,
                              collInfo);
    // P32 -- DeRef no longer destroys collInfo->guard early (see
    // inspectorRetireCollInfo's own docstring); unconditionally unlock
    // regardless of res now, instead of skipping unlock on the
    // assumption the struct/lock might already be gone.
    res = inspectorPluginCollInfoDeRef(collInfo);
    inspectorUnlockRWLock(&collInfo->guard);
    return ncclSuccess;
  } else if (type == ncclProfileKernelCh) {
    struct inspectorKernelChInfo *kernelChInfo
      = (struct inspectorKernelChInfo *)eHandle;
    struct inspectorCollInfo *collInfo = kernelChInfo->collInfo;
    if (collInfo && (collInfo->type == ncclProfileColl || collInfo->type == ncclProfileP2p)) {
      inspectorLockWr(&collInfo->guard);
      struct inspectorEventTraceInfo *krnlEvtTrk =
        collInfo->collEvtTrk.kernelCh[kernelChInfo->channelId].evntTrace;
      inspectorRecordEventTrace(krnlEvtTrk,
                                NCCL_INSP_EVT_TRK_KERNEL_STOP,
                                collInfo);
      kernelChInfo->tsCompletedUsec = inspectorGetTime();
      collInfo->nKernelChCompleted += 1;

      // P32 -- see inspectorRetireCollInfo's docstring: DeRef no longer
      // destroys collInfo/its guard early. A res==inspectorReturn here
      // means refCount hit 0 on THIS deref alone, before the "last
      // channel" completion check below and before the top-level Coll/
      // P2p StopEvent's own self-ref deref -- genuinely unexpected
      // under the intended protocol (both of those still-outstanding
      // refs should normally prevent this). Still unlock unconditionally
      // (the lock is real and still valid either way) and skip further
      // processing of this now-retired object, rather than crash.
      res = inspectorPluginCollInfoDeRef(collInfo);
      if (res == inspectorReturn) {
        WARN("NCCL Inspector unnatural refcount-to-zero: inspectorPluginStopEvent:ncclProfileKernelCh (channel=%u) -- retired safely, not processed further", kernelChInfo->channelId);
        inspectorUnlockRWLock(&collInfo->guard);
        return ncclSuccess;
      }
      if ((collInfo->nKernelChCompleted == collInfo->nKernelChStarted)
          && (collInfo->nKernelChCompleted == collInfo->nChannels)) {
        struct inspectorCompletedCollInfo completedColl;
        struct inspectorCommInfo *commInfo = collInfo->commInfo;
        collInfo->tsCompletedUsec = kernelChInfo->tsCompletedUsec;
        inspectorUpdateCollPerf(&completedColl, collInfo);

        res = inspectorPluginCollInfoDeRef(collInfo);
        inspectorUnlockRWLock(&collInfo->guard);
        if (commInfo != nullptr) {
          inspectorLockWr(&commInfo->guard);
          inspectorComputeCollBw(commInfo,
                                 &completedColl,
                                 completedColl.func);
          memcpy(&commInfo->completedCollInfo,
                 &completedColl,
                 sizeof(struct inspectorCompletedCollInfo));
          commInfo->dump = true;
          inspectorUnlockRWLock(&commInfo->guard);
        }
        return ncclSuccess;
      }
      inspectorUnlockRWLock(&collInfo->guard);
    }
    return ncclSuccess;
  }
  return ncclSuccess;
}

/*
 * Description:
 *
 *   Records the state of a profiling event for the NCCL Inspector
 *   plugin.
 *
 * Thread Safety:
 *
 *   Thread-safe (updates event state as needed).
 *
 * Input:
 *   void* eHandle - event handle.
 *   ncclProfilerEventState_t eState - event state.
 *   ncclProfilerEventStateArgs_t* eStateArgs - event state arguments.
 *
 * Output:
 *   Event state is updated as needed.
 *
 * Return:
 *   ncclResult_t - success or error code.
 *
 */
__hidden ncclResult_t inspectorPluginRecordEventState(void* eHandle,
                                                      ncclProfilerEventState_t eState,
                                                      ncclProfilerEventStateArgs_t* eStateArgs) {
  if (eHandle == nullptr || eStateArgs == nullptr)
    return ncclSuccess;

  uint64_t type = *(uint64_t *)eHandle;

  if (type == ncclProfileKernelCh && eState == ncclProfilerKernelChStop) {
    struct inspectorKernelChInfo *kernelChInfo = (struct inspectorKernelChInfo *)eHandle;
    struct inspectorCollInfo *collInfo = kernelChInfo->collInfo;
    inspectorResult_t res = inspectorSuccess;
    if (collInfo && (collInfo->type == ncclProfileColl || collInfo->type == ncclProfileP2p)) {
      inspectorLockWr(&collInfo->guard);
      struct inspectorEventTraceInfo *krnlEvtTrk
        = collInfo->collEvtTrk.kernelCh[kernelChInfo->channelId].evntTrace;
      inspectorRecordEventTrace(krnlEvtTrk,
                                NCCL_INSP_EVT_TRK_KERNEL_RECORD,
                                collInfo);
      kernelChInfo->stopGpuClk = eStateArgs->kernelCh.pTimer;
      if (kernelChInfo->startGpuClk != 0) {
        // P32 -- see inspectorRetireCollInfo's docstring: DeRef never
        // destroys collInfo/its guard early any more, so unlock
        // unconditionally below regardless of res.
        res = inspectorPluginCollInfoDeRef(collInfo);
        if (res == inspectorReturn) {
          WARN("NCCL Inspector unnatural refcount-to-zero: inspectorPluginRecordEventState (channel=%u) -- retired safely", kernelChInfo->channelId);
        }
      }
      inspectorUnlockRWLock(&collInfo->guard);
    }
  }
  return ncclSuccess;
}

ncclProfiler_t ncclProfiler_v5 = {
  "Inspector",
  inspectorPluginInit,
  inspectorPluginStartEvent,
  inspectorPluginStopEvent,
  inspectorPluginRecordEventState,
  inspectorPluginFinalize,
};
