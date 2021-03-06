#include "gradient_descent_common.cl"


#if USE_ORTHO > 0
#include "weights_ortho.cl"
#endif


__kernel void weights_update(__global const dtype    *_err_output,
                             __global const dtype    *_input,
                             __global dtype          *weights,
                             __global const dtype    *gradient,
                             __global dtype          *accumulated_gradient,
                             __global dtype          *gradient_with_moment,
                             const dtype             lr,
                             const dtype             factor_l12,
                             const dtype             l1_vs_l2,
                             const dtype             moment
#if USE_ORTHO > 0
                             , const dtype           factor_ortho,
                             __global const dtype    *col_sums
#endif
                    ) {
  size_t idx = get_global_id(0);
  dtype sum = gradient[idx];
  #include "weights_update.store_output.cl"
}
