dtype weight = weights[idx];
dtype gd = -lr * (sum + gradient_step_l12(weight, factor_l12, l1_vs_l2)
#if USE_ORTHO > 0
  #if WEIGHTS_TRANSPOSED > 0
           + gradient_step_ortho(weight, factor_ortho, idx / Y, Y, col_sums)
  #else
           + gradient_step_ortho(weight, factor_ortho, idx % H, Y, col_sums)
  #endif
#endif
                  );

#include "gradient_descent.store_output.cu"
