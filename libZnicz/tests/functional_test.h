/*! @file functional_test.h
 *  @brief Functional workflow test using Znicz units.
 *  @author Ernesto Sanches <ernestosanches@gmail.com>
 *  @version 1.0
 *
 *  @section Notes
 *  This code partially conforms to <a href="http://google-styleguide.googlecode.com/svn/trunk/cppguide.xml">Google C++ Style Guide</a>.
 *
 *  @section Copyright
 *  Copyright 2013 Samsung R&D Institute Russia
 */

#ifndef TESTS_FUNCTIONAL_TEST_H_
#define TESTS_FUNCTIONAL_TEST_H_

#include <tests/common_test.h>
#include <veles/workflow.h>

/** @brief Functional test of the workflow containing two neural network layers
 *  with different activation functions and number of neurons.
 */
class WorkflowTest : public CommonTest {
 protected:
  static const size_t kInputsLinear;
  static const size_t kOutputsLinear;
  static const size_t kOutputsTanh;

  virtual void SetUp() override;
  std::shared_ptr<veles::Workflow> workflow() {
    if (!workflow_) {
      workflow_ = std::make_shared<veles::Workflow>();
    }
    return workflow_;
  }
  std::shared_ptr<float> input() {
    if (!input_) {
      input_ = CreateFloatArray(kInputsLinear, kValueInputInit);
    }
    return input_;
  }
  std::shared_ptr<float> output() {
    if (!output_) {
      output_ = CreateFloatArray(kOutputsTanh, kValueOutputInit);
    }
    return output_;
  }

 private:
  std::shared_ptr<veles::Workflow> workflow_;
  std::shared_ptr<float> input_;
  std::shared_ptr<float> output_;
};

#endif  // TESTS_FUNCTIONAL_TEST_H_
