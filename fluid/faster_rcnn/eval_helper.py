import os
import numpy as np
import paddle.fluid as fluid
import math
import box_utils


def box_decoder(target_box, prior_box, prior_box_var):
    proposals = np.zeros_like(target_box, dtype=np.float32)
    prior_box_loc = np.zeros_like(prior_box, dtype=np.float32)
    prior_box_loc[:, 0] = prior_box[:, 2] - prior_box[:, 0] + 1.
    prior_box_loc[:, 1] = prior_box[:, 3] - prior_box[:, 1] + 1.
    prior_box_loc[:, 2] = (prior_box[:, 2] + prior_box[:, 0]) / 2
    prior_box_loc[:, 3] = (prior_box[:, 3] + prior_box[:, 1]) / 2
    pred_bbox = np.zeros_like(target_box, dtype=np.float32)
    for i in range(prior_box.shape[0]):
        dw = np.minimum(prior_box_var[2] * target_box[i, 2::4],
                        np.log(1000. / 16.))
        dh = np.minimum(prior_box_var[3] * target_box[i, 3::4],
                        np.log(1000. / 16.))
        pred_bbox[i, 0::4] = prior_box_var[0] * target_box[
            i, 0::4] * prior_box_loc[i, 0] + prior_box_loc[i, 2]
        pred_bbox[i, 1::4] = prior_box_var[1] * target_box[
            i, 1::4] * prior_box_loc[i, 1] + prior_box_loc[i, 3]
        pred_bbox[i, 2::4] = np.exp(dw) * prior_box_loc[i, 0]
        pred_bbox[i, 3::4] = np.exp(dh) * prior_box_loc[i, 1]
    proposals[:, 0::4] = pred_bbox[:, 0::4] - pred_bbox[:, 2::4] / 2
    proposals[:, 1::4] = pred_bbox[:, 1::4] - pred_bbox[:, 3::4] / 2
    proposals[:, 2::4] = pred_bbox[:, 0::4] + pred_bbox[:, 2::4] / 2 - 1
    proposals[:, 3::4] = pred_bbox[:, 1::4] + pred_bbox[:, 3::4] / 2 - 1

    return proposals


def clip_tiled_boxes(boxes, im_shape):
    """Clip boxes to image boundaries. im_shape is [height, width] and boxes
    has shape (N, 4 * num_tiled_boxes)."""
    assert boxes.shape[1] % 4 == 0, \
        'boxes.shape[1] is {:d}, but must be divisible by 4.'.format(
        boxes.shape[1]
    )
    # x1 >= 0
    boxes[:, 0::4] = np.maximum(np.minimum(boxes[:, 0::4], im_shape[1] - 1), 0)
    # y1 >= 0
    boxes[:, 1::4] = np.maximum(np.minimum(boxes[:, 1::4], im_shape[0] - 1), 0)
    # x2 < im_shape[1]
    boxes[:, 2::4] = np.maximum(np.minimum(boxes[:, 2::4], im_shape[1] - 1), 0)
    # y2 < im_shape[0]
    boxes[:, 3::4] = np.maximum(np.minimum(boxes[:, 3::4], im_shape[0] - 1), 0)
    return boxes


def get_nmsed_box(args, rpn_rois, confs, locs, class_nums, im_info,
                  numId_to_catId_map):
    lod = rpn_rois.lod()[0]
    rpn_rois_v = np.array(rpn_rois)
    variance_v = np.array([0.1, 0.1, 0.2, 0.2])
    confs_v = np.array(confs)
    locs_v = np.array(locs)
    rois = box_decoder(locs_v, rpn_rois_v, variance_v)
    im_results = [[] for _ in range(len(lod) - 1)]
    new_lod = [0]
    for i in range(len(lod) - 1):
        start = lod[i]
        end = lod[i + 1]
        if start == end:
            continue
        rois_n = rois[start:end, :]
        rois_n = rois_n / im_info[i][2]
        rois_n = clip_tiled_boxes(rois_n, im_info[i][:2])

        cls_boxes = [[] for _ in range(class_nums)]
        scores_n = confs_v[start:end, :]
        for j in range(1, class_nums):
            inds = np.where(scores_n[:, j] > args.score_threshold)[0]
            scores_j = scores_n[inds, j]
            rois_j = rois_n[inds, j * 4:(j + 1) * 4]
            dets_j = np.hstack((rois_j, scores_j[:, np.newaxis])).astype(
                np.float32, copy=False)
            keep = box_utils.nms(dets_j, args.nms_threshold)
            nms_dets = dets_j[keep, :]
            #add labels
            cat_id = numId_to_catId_map[j]
            label = np.array([cat_id for _ in range(len(keep))])
            nms_dets = np.hstack((nms_dets, label[:, np.newaxis])).astype(
                np.float32, copy=False)
            cls_boxes[j] = nms_dets
    # Limit to max_per_image detections **over all classes**
        image_scores = np.hstack(
            [cls_boxes[j][:, -2] for j in range(1, class_nums)])
        if len(image_scores) > 100:
            image_thresh = np.sort(image_scores)[-100]
            for j in range(1, class_nums):
                keep = np.where(cls_boxes[j][:, -2] >= image_thresh)[0]
                cls_boxes[j] = cls_boxes[j][keep, :]

        im_results_n = np.vstack([cls_boxes[j] for j in range(1, class_nums)])
        im_results[i] = im_results_n
        new_lod.append(len(im_results_n) + new_lod[-1])
        boxes = im_results_n[:, :-2]
        scores = im_results_n[:, -2]
        labels = im_results_n[:, -1]
    im_results = np.vstack([im_results[k] for k in range(len(lod) - 1)])
    return new_lod, im_results
