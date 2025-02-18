#  ------------------------------------------------------------------------------------------
#  Copyright (c) Microsoft Corporation. All rights reserved.
#  Licensed under the MIT License (MIT). See LICENSE in the repo root for license information.
#  ------------------------------------------------------------------------------------------
from typing import Any, Dict, List, Tuple

import torch
from pytorch_lightning.utilities import move_data_to_device
from torch.nn import ModuleDict, ModuleList

from InnerEye.Common.common_util import SUBJECT_METRICS_FILE_NAME
from InnerEye.Common.metrics_constants import LoggingColumns, MetricType, TRAIN_PREFIX, VALIDATION_PREFIX
from InnerEye.ML.common import ModelExecutionMode
from InnerEye.ML.config import SegmentationModelBase
from InnerEye.ML.dataset.sample import CroppedSample
from InnerEye.ML.dataset.scalar_sample import ScalarItem
from InnerEye.ML.lightning_base import InnerEyeLightning
from InnerEye.ML.lightning_metrics import Accuracy05, AccuracyAtOptimalThreshold, AreaUnderPrecisionRecallCurve, \
    AreaUnderRocCurve, BinaryCrossEntropyWithLogits, ExplainedVariance, FalseNegativeRateOptimalThreshold, \
    FalsePositiveRateOptimalThreshold, MeanAbsoluteError, MeanSquaredError, MetricForMultipleStructures, \
    OptimalThreshold, ScalarMetricsBase
from InnerEye.ML.metrics import compute_dice_across_patches
from InnerEye.ML.metrics_dict import DataframeLogger, MetricsDict, SequenceMetricsDict
from InnerEye.ML.scalar_config import ScalarModelBase
from InnerEye.ML.sequence_config import SequenceModelBase
from InnerEye.ML.utils import image_util, metrics_util, model_util
from InnerEye.ML.utils.model_util import get_scalar_model_inputs_and_labels
from InnerEye.ML.utils.sequence_utils import apply_sequence_model_loss, get_masked_model_outputs_and_labels

SUBJECT_OUTPUT_PER_RANK_PREFIX = f"{SUBJECT_METRICS_FILE_NAME}.rank"


class SegmentationLightning(InnerEyeLightning):
    """
    This class implements 3D segmentation models with PyTorch Lightning.
    """

    def __init__(self, config: SegmentationModelBase, *args: Any, **kwargs: Any) -> None:
        super().__init__(config, *args, **kwargs)
        self.model = config.create_model()
        self.loss_fn = model_util.create_segmentation_loss_function(config)
        self.ground_truth_ids = config.ground_truth_ids
        self.train_dice = MetricForMultipleStructures(ground_truth_ids=self.ground_truth_ids, is_training=True)
        self.val_dice = MetricForMultipleStructures(ground_truth_ids=self.ground_truth_ids, is_training=False)
        self.train_voxels = MetricForMultipleStructures(ground_truth_ids=self.ground_truth_ids, is_training=True,
                                                        metric_name=MetricType.VOXEL_COUNT.value,
                                                        use_average_across_structures=False)
        self.val_voxels = MetricForMultipleStructures(ground_truth_ids=self.ground_truth_ids, is_training=False,
                                                      metric_name=MetricType.VOXEL_COUNT.value,
                                                      use_average_across_structures=False)

    def forward(self, patches: torch.Tensor) -> torch.Tensor:  # type: ignore
        """
        Runs a set of 3D crops through the segmentation model, and returns the result. This method is used
        at inference time.
        :param patches: A tensor of size [batches, channels, Z, Y, X]
        """
        return self.logits_to_posterior(self.model(patches))

    def logits_to_posterior(self, logits: torch.Tensor) -> torch.Tensor:
        """
        Apply Softmax on dimension 1 (Class) to map model output into a posterior probability distribution [0,1]
        """
        return torch.nn.functional.softmax(logits, dim=1)

    def training_or_validation_step(self,
                                    sample: Dict[str, Any],
                                    batch_index: int,
                                    is_training: bool) -> torch.Tensor:
        """
        Runs training for a single minibatch of training or validation data, and computes all metrics.
        :param is_training: If true, the method is called from `training_step`, otherwise it is called from
        `validation_step`.
        :param sample: The batched sample on which the model should be trained.
        :param batch_index: The index of the present batch (supplied only for diagnostics).
        """
        cropped_sample: CroppedSample = CroppedSample.from_dict(sample=sample)
        # Forward propagation can lead to a model output that is smaller than the input image (crop).
        # labels_center_crop is the relevant part of the labels tensor that the model will actually produce.
        labels = cropped_sample.labels_center_crop

        mask = cropped_sample.mask_center_crop if is_training else None
        if is_training:
            logits = self.model(cropped_sample.image)
        else:
            with torch.no_grad():
                logits = self.model(cropped_sample.image)
        loss = self.loss_fn(logits, labels)

        # apply Softmax on dimension 1 (Class) to map model output into a posterior probability distribution [0,1]
        posteriors = self.logits_to_posterior(logits)

        # apply mask if required
        if mask is not None:
            posteriors = image_util.apply_mask_to_posteriors(posteriors=posteriors, mask=mask)

        # post process posteriors to compute result
        segmentation = image_util.posteriors_to_segmentation(posteriors=posteriors)
        self.compute_metrics(cropped_sample, segmentation, is_training)

        self.write_loss(is_training, loss)
        return loss

    def compute_metrics(self, cropped_sample: CroppedSample, segmentation: torch.Tensor,
                        is_training: bool) -> None:
        """
        Computes and stores all metrics coming out of a single training step.
        :param cropped_sample: The batched image crops used for training or validation.
        :param segmentation: The segmentation that was produced by the model.
        """
        # dice_per_crop_and_class has one row per crop, with background class removed
        # Dice NaN means that both ground truth and prediction are empty.
        dice_per_crop_and_class = compute_dice_across_patches(
            segmentation=segmentation,
            ground_truth=cropped_sample.labels_center_crop,
            allow_multiple_classes_for_each_pixel=True)[:, 1:]
        # Number of foreground voxels per class, across all crops
        foreground_voxels = metrics_util.get_number_of_voxels_per_class(cropped_sample.labels)[:, 1:]
        # Store Dice and voxel count per sample in the minibatch. We need a custom aggregation logic for Dice
        # because it can be NaN. Also use custom logging for voxel count because Lightning's batch-size weighted
        # average has a bug.
        for i in range(dice_per_crop_and_class.shape[0]):
            dice = self.train_dice if is_training else self.val_dice
            dice.update(dice_per_crop_and_class[i, :])
            voxel_count = self.train_voxels if is_training else self.val_voxels
            voxel_count.update(foreground_voxels[i, :])
        # store diagnostics per batch
        center_indices = cropped_sample.center_indices
        if isinstance(center_indices, torch.Tensor):
            center_indices = center_indices.cpu().numpy()
        if is_training:
            self.train_diagnostics.append(center_indices)
        else:
            self.val_diagnostics.append(center_indices)
        # if self.train_val_params.in_training_mode:
        #     # store the sample train patch from this epoch for visualization
        #     if batch_index == self.example_to_save and self.config.store_dataset_sample:
        #         _store_dataset_sample(self.config, self.train_val_params.epoch, forward_pass_result,
        #                               cropped_sample)
        num_subjects = cropped_sample.image.shape[0]
        self.log_on_epoch(name=MetricType.SUBJECT_COUNT,
                          value=num_subjects,
                          is_training=is_training,
                          reduce_fx=sum,
                          sync_dist_op=None)

    def training_or_validation_epoch_end(self, is_training: bool) -> None:
        """
        Writes all training or validation metrics that were aggregated over the epoch to the loggers.
        """
        dice = self.train_dice if is_training else self.val_dice
        for name, value in dice.compute_all():
            self.log(name, value)
        dice.reset()
        voxel_count = self.train_voxels if is_training else self.val_voxels
        for name, value in voxel_count.compute_all():
            self.log(name, value)
        voxel_count.reset()
        super().training_or_validation_epoch_end(is_training=is_training)


def get_subject_output_file_per_rank(rank: int) -> str:
    """
    Gets the name of a file that will store the per-rank per-subject model outputs.
    :param rank: The rank of the current model in distributed training.
    :return: A string like "rank7_metrics.csv"
    """
    return f"{SUBJECT_OUTPUT_PER_RANK_PREFIX}{rank}"


class ScalarLightning(InnerEyeLightning):
    """
    This class implements training of classification, regression, and sequence models with PyTorch Lightning.
    """

    def __init__(self, config: ScalarModelBase, *args: Any, **kwargs: Any) -> None:
        super().__init__(config, *args, **kwargs)
        self.model = config.create_model()
        raw_loss = model_util.create_scalar_loss_function(config)
        if isinstance(config, SequenceModelBase):
            self.loss_fn = lambda model_output, loss: apply_sequence_model_loss(raw_loss, model_output, loss)
            self.target_indices = config.get_target_indices()
            self.target_names = [SequenceMetricsDict.get_hue_name_from_target_index(p)
                                 for p in config.sequence_target_positions]
        else:
            self.loss_fn = raw_loss
            self.target_indices = []
            self.target_names = config.class_names
        self.is_classification_model = config.is_classification_model
        self.use_mean_teacher_model = config.compute_mean_teacher_model
        self.is_binary_classification_or_regression = True if len(config.class_names) == 1 else False
        self.logits_to_posterior_fn = config.get_post_loss_logits_normalization_function()
        self.loss_type = config.loss_type
        # These two fields store the PyTorch Lightning Metrics objects that will compute metrics on validation
        # and training set, in particular ones that are not possible to compute from a single minibatch (AUC and alike)
        self.train_metric_computers = self.create_metric_computers()
        self.val_metric_computers = self.create_metric_computers()

        # if config.compute_grad_cam:
        #     model_to_evaluate = self.train_val_params.mean_teacher_model if \
        #         config.compute_mean_teacher_model else self.train_val_params.model
        #     self.guided_grad_cam = VisualizationMaps(model_to_evaluate, config)
        #     config.visualization_folder.mkdir(exist_ok=True)

    def create_metric_computers(self) -> ModuleDict:
        """
        Gets a set of objects that compute all the metrics for the type of model that is being trained,
        across all prediction targets (sequence positions when using a sequence model).
        :return: A dictionary mapping from names of prediction targets to a list of metric computers.
        """
        # The metric computers should be stored in an object that derives from torch.Module,
        # so that they are picked up when moving the whole LightningModule to GPU.
        # https://github.com/PyTorchLightning/pytorch-lightning/issues/4713
        return ModuleDict({p: self._get_metrics_computers() for p in self.target_names})

    def _get_metrics_computers(self) -> ModuleList:
        """
        Gets the objects that compute metrics for the present kind of models, for a single prediction target.
        """
        if self.is_classification_model:
            return ModuleList([Accuracy05(),
                               AccuracyAtOptimalThreshold(),
                               OptimalThreshold(),
                               FalsePositiveRateOptimalThreshold(),
                               FalseNegativeRateOptimalThreshold(),
                               AreaUnderRocCurve(),
                               AreaUnderPrecisionRecallCurve(),
                               BinaryCrossEntropyWithLogits()])
        else:
            return ModuleList([MeanAbsoluteError(), MeanSquaredError(), ExplainedVariance()])

    def forward(self, *model_inputs: torch.Tensor) -> torch.Tensor:  # type: ignore
        """
        Runs a list of model input tensors through the model and returns the results.
        """
        return self.logits_to_posterior(self.model(*model_inputs))

    def logits_to_posterior(self, logits: torch.Tensor) -> torch.Tensor:
        """
        Apply the model-specific normalization to go from logits (model outputs) to posteriors.
        """
        return self.logits_to_posterior_fn(logits)

    def on_train_start(self) -> None:
        """
        Initializes the per-rank logger objects that write to the file system.
        """
        # These loggers store the per-subject model outputs. They cannot be initialized in the constructor because
        # the trainer object will not yet be set, and we need to get the rank from there.
        fixed_logger_columns = {LoggingColumns.CrossValidationSplitIndex.value: self.cross_validation_split_index}
        subject_output_file = get_subject_output_file_per_rank(self.trainer.global_rank)
        self.train_subject_outputs_logger = DataframeLogger(self.train_metrics_folder / subject_output_file,
                                                            fixed_columns=fixed_logger_columns)
        self.val_subject_outputs_logger = DataframeLogger(self.val_metrics_folder / subject_output_file,
                                                          fixed_columns=fixed_logger_columns)

    def training_or_validation_step(self,
                                    sample: Dict[str, Any],
                                    batch_index: int,
                                    is_training: bool) -> torch.Tensor:
        """
        Runs training for a single minibatch of training or validation data, and computes all metrics.
        :param is_training: If true, the method is called from `training_step`, otherwise it is called from
        `validation_step`.
        :param sample: The batched sample on which the model should be trained.
        :param batch_index: The index of the present batch (supplied only for diagnostics).
        Runs a minibatch of training or validation data through the model.
        """
        model_inputs_and_labels = get_scalar_model_inputs_and_labels(self.model, self.target_indices, sample)
        labels = model_inputs_and_labels.labels
        if is_training:
            logits = self.model(*model_inputs_and_labels.model_inputs)
        else:
            with torch.no_grad():
                logits = self.model(*model_inputs_and_labels.model_inputs)
        subject_ids = model_inputs_and_labels.subject_ids
        loss = self.loss_fn(logits, labels)
        self.write_loss(is_training, loss)
        self.compute_and_log_metrics(logits, labels, subject_ids, is_training)
        self.log_on_epoch(name=MetricType.SUBJECT_COUNT,
                          value=len(model_inputs_and_labels.subject_ids),
                          is_training=is_training,
                          reduce_fx=sum)
        return loss

    def compute_and_log_metrics(self,
                                logits: torch.Tensor,
                                targets: torch.Tensor,
                                subject_ids: List[str],
                                is_training: bool) -> None:
        """
        Computes all the metrics for a given (logits, labels) pair, and writes them to the loggers.
        :param logits: The model output before normalization.
        :param targets: The expected model outputs.
        :param subject_ids: The subject IDs for the present minibatch.
        :param is_training: If True, write the metrics as training metrics, otherwise as validation metrics.
        :return:
        """
        metrics = self.train_metric_computers if is_training else self.val_metric_computers
        per_subject_outputs: List[Tuple[str, str, torch.Tensor, torch.Tensor]] = []
        for i, (prediction_target, metric_list) in enumerate(metrics.items()):
            # mask the model outputs and labels if required
            masked = get_masked_model_outputs_and_labels(
                logits[:, i, ...], targets[:, i, ...], subject_ids)
            # compute metrics on valid masked tensors only
            if masked is not None:
                _logits = masked.model_outputs.data
                _posteriors = self.logits_to_posterior(_logits)
                # Classification metrics expect labels as integers, but they are float throughout the rest of the code
                labels_dtype = torch.int if self.is_classification_model else _posteriors.dtype
                _labels = masked.labels.data.to(dtype=labels_dtype)
                _subject_ids = masked.subject_ids
                assert _subject_ids is not None
                for metric in metric_list:
                    if isinstance(metric, ScalarMetricsBase) and metric.compute_from_logits:
                        metric(_logits, _labels)
                    else:
                        metric(_posteriors, _labels)
                per_subject_outputs.extend(
                    zip(_subject_ids, [prediction_target] * len(_subject_ids), _posteriors.tolist(), _labels.tolist()))
        # Write a full breakdown of per-subject predictions and labels to a file. These files are local to the current
        # rank in distributed training, and will be aggregated after training.
        logger = self.train_subject_outputs_logger if is_training else self.val_subject_outputs_logger
        data_split = ModelExecutionMode.TRAIN if is_training else ModelExecutionMode.VAL
        for subject, prediction_target, model_output, label in per_subject_outputs:
            logger.add_record({
                LoggingColumns.Epoch.value: self.current_epoch,
                LoggingColumns.Patient.value: subject,
                LoggingColumns.Hue.value: prediction_target,
                LoggingColumns.ModelOutput.value: model_output,
                LoggingColumns.Label.value: label,
                LoggingColumns.DataSplit.value: data_split.value
            })

    def training_or_validation_epoch_end(self, is_training: bool) -> None:
        """
        Writes all training or validation metrics that were aggregated over the epoch to the loggers.
        """
        metric_computers = self.train_metric_computers if is_training else self.val_metric_computers
        prefix = TRAIN_PREFIX if is_training else VALIDATION_PREFIX
        for prediction_target, metric_list in metric_computers.items():
            target_suffix = "" if (prediction_target == MetricsDict.DEFAULT_HUE_KEY
                                   or self.is_binary_classification_or_regression) else f"/{prediction_target}"
            for metric in metric_list:
                if metric.has_predictions:
                    # Sequence models can have no predictions at all for particular positions, depending on the data.
                    # Hence, only log if anything has been accumulated.
                    self.log(name=prefix + metric.name + target_suffix, value=metric.compute())
                    metric.reset()
        logger = self.train_subject_outputs_logger if is_training else self.val_subject_outputs_logger
        logger.flush()
        super().training_or_validation_epoch_end(is_training)

    def transfer_batch_to_device(self, batch: Any, device: torch.device) -> Any:  # type: ignore
        """
        For sequence models, transfer the nested lists of items to the given GPU device.
        For all other models, this relies on the superclass to move the batch of data to the GPU.
        :param batch: A batch of data coming from the dataloader.
        :param device: The target CUDA device.
        :return: A modified batch of data, where all tensor now live on the given CUDA device.
        """
        return transfer_batch_to_device(batch, device)


def transfer_batch_to_device(batch: Any, device: torch.device) -> Any:
    """
    For sequence models, transfer the nested lists of items to the given GPU device.
    For all other models, this relies on Lightning's default code to move the batch of data to the GPU.
    :param batch: A batch of data coming from the dataloader.
    :param device: The target CUDA device.
    :return: A modified batch of data, where all tensor now live on the given CUDA device.
    """
    if not isinstance(batch, dict):
        raise ValueError(f"This function expects a dictionary input, but got: {type(batch)}")
    # For sequence models, this method receives a dictionary with "item": List[List[ScalarItem]]
    items = batch.get("items", None)
    if items is not None and isinstance(items, List) and isinstance(items[0], List) and \
            isinstance(items[0][0], ScalarItem):
        batch["items"] = [[j.to_device(device) for j in i] for i in items]
        return batch
    else:
        return move_data_to_device(batch, device)
