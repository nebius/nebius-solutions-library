/* P23 (single-rank AllToAll fault, Candidate B) -- LD_PRELOAD shim.
 * Hooks the real, exported ibv_modify_qp(). ibv_modify_qp_rate_limit()
 * itself is a `static inline` function defined directly in verbs.h (it
 * dispatches through the provider's own vtable via
 * verbs_get_ctx_op(...)->modify_qp_rate_limit), not a separate exported
 * symbol -- so there is nothing to dlsym() for it; this file just
 * #includes verbs.h and calls it directly, the same way any real verbs
 * application would.
 *
 * Real QPs transition RESET -> INIT -> RTR -> RTS via successive
 * ibv_modify_qp() calls (performed internally by NCCL's own connection
 * setup, not by this shim). Rate limiting is a send-side property that
 * only meaningfully applies once a QP reaches RTS (ready-to-send,
 * meaning the connection is actually established) -- applying it any
 * earlier (e.g. immediately after ibv_create_qp(), while still in
 * RESET) has no defined effect. So this hook lets the REAL
 * ibv_modify_qp() run first (never altering NCCL's own connection
 * setup), and only once that call successfully transitions a QP TO
 * RTS does it immediately apply the rate limit -- "immediately after
 * connection establishment", per the design this session is testing.
 *
 * Loaded via LD_PRELOAD for ONE rank's process only (via the per-rank
 * launch wrapper); every other rank's process never loads this at all,
 * so their QPs are completely unaffected -- the asymmetry is in WHICH
 * QPs get throttled, not in any timing of when ranks enter collectives.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <infiniband/verbs.h>

static int (*real_ibv_modify_qp)(struct ibv_qp *, struct ibv_qp_attr *, int) = NULL;

int ibv_modify_qp(struct ibv_qp *qp, struct ibv_qp_attr *attr, int attr_mask) {
    fprintf(stderr, "[qp_rate_limit_shim] ENTER ibv_modify_qp qp=%p attr_mask=0x%x\n", (void*)qp, attr_mask);
    fflush(stderr);
    if (!real_ibv_modify_qp) {
        real_ibv_modify_qp = dlsym(RTLD_NEXT, "ibv_modify_qp");
        fprintf(stderr, "[qp_rate_limit_shim] RTLD_NEXT resolved real_ibv_modify_qp=%p\n", (void*)real_ibv_modify_qp);
        fflush(stderr);
        if (!real_ibv_modify_qp) {
            /* RTLD_NEXT failed -- the real symbol isn't visible in the
             * global scope chain after this shim (a real, structural
             * gotcha with how UCX/mlx5 provider plugins load
             * libibverbs, not a bug in the interposition itself, which
             * clearly IS working since this hook is being called at
             * all). Try explicitly finding an already-loaded handle for
             * the real library instead of relying on scope ordering. */
            void *h = dlopen("libibverbs.so.1", RTLD_NOW | RTLD_NOLOAD);
            fprintf(stderr, "[qp_rate_limit_shim] RTLD_NOLOAD handle=%p\n", h);
            fflush(stderr);
            if (h) {
                real_ibv_modify_qp = dlsym(h, "ibv_modify_qp");
                fprintf(stderr, "[qp_rate_limit_shim] via RTLD_NOLOAD handle resolved=%p\n", (void*)real_ibv_modify_qp);
                fflush(stderr);
            }
        }
        if (!real_ibv_modify_qp) {
            fprintf(stderr, "[qp_rate_limit_shim] FATAL: could not find real ibv_modify_qp by any method\n");
            fflush(stderr);
            return -1;
        }
    }
    int ret = real_ibv_modify_qp(qp, attr, attr_mask);
    fprintf(stderr, "[qp_rate_limit_shim] real_ibv_modify_qp returned %d\n", ret);
    fflush(stderr);
    if (ret == 0 && (attr_mask & IBV_QP_STATE) && attr->qp_state == IBV_QPS_RTS) {
        struct ibv_qp_rate_limit_attr rl;
        memset(&rl, 0, sizeof(rl));
        const char *rate_env = getenv("QP_RATE_LIMIT_KBPS");
        rl.rate_limit = rate_env ? (uint32_t)atoi(rate_env) : 1000000; /* kbps; 1,000,000 = 1 Gbps */
        rl.max_burst_sz = 65536;
        rl.typical_pkt_sz = 4096;
        fprintf(stderr, "[qp_rate_limit_shim] qp=%p reached RTS, about to call ibv_modify_qp_rate_limit\n", (void*)qp);
        fflush(stderr);
        int rc = ibv_modify_qp_rate_limit(qp, &rl);
        fprintf(stderr, "[qp_rate_limit_shim] qp=%p qp_num=%u -> RTS, rate_limit=%u kbps applied, rc=%d (%s)\n",
                (void*)qp, qp->qp_num, rl.rate_limit, rc, rc == 0 ? "OK" : strerror(rc));
        fflush(stderr);
    }
    return ret;
}
