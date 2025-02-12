#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import logging
import time
from typing import Any
from typing import Dict
from typing import Iterable
from typing import Optional
from typing import Sequence

from google.api_core.exceptions import ClientError
from google.api_core.exceptions import TooManyRequests
from google.cloud import aiplatform

from apache_beam.io.components.adaptive_throttler import AdaptiveThrottler
from apache_beam.metrics.metric import Metrics
from apache_beam.ml.inference import utils
from apache_beam.ml.inference.base import ModelHandler
from apache_beam.ml.inference.base import PredictionResult
from apache_beam.utils import retry

MSEC_TO_SEC = 1000

LOGGER = logging.getLogger("VertexAIModelHandlerJSON")

# pylint: disable=line-too-long


def _retry_on_gcp_client_error(exception):
  """
  Retry filter that returns True if a returned HTTP error code is 4xx. This is
  used to retry remote requests that fail, most notably 429 (TooManyRequests.)
  This is used for GCP-specific client errors.

  Args:
    exception: the returned exception encountered during the request/response
      loop.

  Returns:
    boolean indication whether or not the exception is a GCP ClientError.
  """
  return isinstance(exception, ClientError)


class VertexAIModelHandlerJSON(ModelHandler[Any,
                                            PredictionResult,
                                            aiplatform.Endpoint]):
  def __init__(
      self,
      endpoint_id: str,
      project: str,
      location: str,
      experiment: Optional[str] = None,
      **kwargs):
    """Implementation of the ModelHandler interface for Vertex AI.
    **NOTE:** This API and its implementation are under development and
    do not provide backward compatibility guarantees.
    Unlike other ModelHandler implementations, this does not load the model
    being used onto the worker and instead makes remote queries to a
    Vertex AI endpoint. In that way it functions more like a mid-pipeline
    IO. At present this implementation only supports public endpoints with
    a maximum request size of 1.5 MB.
    Args:
      endpoint_id: the numerical ID of the Vertex AI endpoint to query
      project: the GCP project name where the endpoint is deployed
      location: the GCP location where the endpoint is deployed
      experiment (Optional): experiment label to apply to the queries
    """

    self._env_vars = kwargs.get('env_vars', {})
    # TODO: support the full list of options for aiplatform.init()
    # See https://cloud.google.com/python/docs/reference/aiplatform/latest/google.cloud.aiplatform#google_cloud_aiplatform_init
    aiplatform.init(project=project, location=location, experiment=experiment)

    # Check for liveness here but don't try to actually store the endpoint
    # in the class yet
    self.endpoint_name = endpoint_id
    _ = self._retrieve_endpoint(self.endpoint_name)

    # Configure AdaptiveThrottler and throttling metrics for client-side
    # throttling behavior.
    # See https://docs.google.com/document/d/1ePorJGZnLbNCmLD9mR7iFYOdPsyDA1rDnTpYnbdrzSU/edit?usp=sharing
    # for more details.
    self.throttled_secs = Metrics.counter(
        VertexAIModelHandlerJSON, "cumulativeThrottlingSeconds")
    self.throttler = AdaptiveThrottler(
        window_ms=1, bucket_ms=1, overload_ratio=2)

  def _retrieve_endpoint(self, endpoint_id: str) -> aiplatform.Endpoint:
    """Retrieves an AI Platform endpoint and queries it for liveness/deployed
    models.

    Args:
      endpoint_id: the numerical ID of the Vertex AI endpoint to retrieve.
    Returns:
      An aiplatform.Endpoint object
    Raises:
      ValueError: if endpoint is inactive or has no models deployed to it.
    """
    endpoint = aiplatform.Endpoint(endpoint_name=endpoint_id)

    try:
      mod_list = endpoint.list_models()
    except Exception as e:
      raise ValueError(
          "Failed to contact endpoint %s, got exception: %s", endpoint_id, e)

    if len(mod_list) == 0:
      raise ValueError("Endpoint %s has no models deployed to it.")

    return endpoint

  def load_model(self) -> aiplatform.Endpoint:
    """Loads the Endpoint object used to build and send prediction request to
    Vertex AI.
    """
    # Check to make sure the endpoint is still active since pipeline
    # construction time
    ep = self._retrieve_endpoint(self.endpoint_name)
    return ep

  @retry.with_exponential_backoff(
      num_retries=5, retry_filter=_retry_on_gcp_client_error)
  def get_request(
      self,
      batch: Sequence[Any],
      model: aiplatform.Endpoint,
      throttle_delay_secs: int,
      inference_args: Optional[Dict[str, Any]]):
    while self.throttler.throttle_request(time.time() * MSEC_TO_SEC):
      LOGGER.info(
          "Delaying request for %d seconds due to previous failures",
          throttle_delay_secs)
      time.sleep(throttle_delay_secs)
      self.throttled_secs.inc(throttle_delay_secs)

    try:
      req_time = time.time()
      prediction = model.predict(
          instances=list(batch), parameters=inference_args)
      self.throttler.successful_request(req_time * MSEC_TO_SEC)
      return prediction
    except TooManyRequests as e:
      LOGGER.warning("request was limited by the service with code %i", e.code)
      raise
    except ClientError as e:
      LOGGER.warning("request failed with error code %i", e.code)
      raise
    except Exception as e:
      LOGGER.error("unexpected exception raised as part of request, got %s", e)
      raise

  def run_inference(
      self,
      batch: Sequence[Any],
      model: aiplatform.Endpoint,
      inference_args: Optional[Dict[str, Any]] = None
  ) -> Iterable[PredictionResult]:
    """ Sends a prediction request to a Vertex AI endpoint containing batch
    of inputs and matches that input with the prediction response from
    the endpoint as an iterable of PredictionResults.

    Args:
      batch: a sequence of any values to be passed to the Vertex AI endpoint.
        Should be encoded as the model expects.
      model: an aiplatform.Endpoint object configured to access the desired
        model.
      inference_args: any additional arguments to send as part of the
        prediction request.

    Returns:
      An iterable of Predictions.
    """

    # Endpoint.predict returns a Prediction type with the prediction values
    # along with model metadata
    prediction = self.get_request(
        batch, model, throttle_delay_secs=5, inference_args=inference_args)

    return utils._convert_to_result(
        batch, prediction.predictions, prediction.deployed_model_id)
