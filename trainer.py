import numpy as np
from torch import nn
from rpn_utils import generate_anchor_loc_label, generate_training_anchors
from torch.autograd import Variable

from convert_label import text_to_num
from rpn_utils import rpn_loss
from fast_rcnn_utils import fast_rcnn_loss


class FasterRCNNTrainer(nn.Module):
    def __init__(self, faster_rcnn, rpn_sigma=3, roi_sigma=1):
        super(FasterRCNNTrainer, self).__init__()
        self.faster_rcnn = faster_rcnn
        self.rpn_sigma = rpn_sigma
        self.roi_sigma = roi_sigma
        self.loc_normalize_mean = faster_rcnn.loc_normalize_mean
        self.loc_normalize_std = faster_rcnn.loc_normalize_std
        self.optimizer = self.faster_rcnn.optimizer

    def forward(self, img_tensor, img_info):
        from timeit import default_timer as timer
        a = timer()
        img_size = img_info['img_size']
        features = self.faster_rcnn.extractor(img_tensor)
        print('features', timer() - a)
        gt_bbox = np.array(img_info['objects'])[:, 1:5].astype(np.float32)
        gt_label = np.array(img_info['objects'])[:, 0]
        gt_label = text_to_num(gt_label)
        a = timer()
        rpn_locs, rpn_scores, rois, anchors = \
            self.faster_rcnn.rpn(features, img_size)
        print('rpn', timer() - a)
        a = timer()
        # RPN loss
        gt_rpn_label, gt_rpn_loc = generate_anchor_loc_label(anchors, gt_bbox, img_size)
        gt_rpn_label = Variable(gt_rpn_label)
        gt_rpn_loc = Variable(gt_rpn_loc)
        rpn_cls_loss, rpn_loc_loss = rpn_loss(rpn_scores, rpn_locs,
                                              gt_rpn_loc, gt_rpn_label,
                                              self.rpn_sigma)
        print('RPN LOSS', timer()-a)
        a = timer()
        # generate proposals from rpn rois
        sampled_roi, gt_roi_loc, gt_roi_label = generate_training_anchors(rois, gt_bbox, gt_label)
        roi_cls_loc, roi_score = self.faster_rcnn.head(features, sampled_roi)
        print('generate proposals from rpn rois', timer() - a)
        a = timer()
        # ROI loss
        roi_cls_loss, roi_loc_loss = fast_rcnn_loss(roi_score, roi_cls_loc,
                                                    gt_roi_loc, gt_roi_label,
                                                    self.roi_sigma)
        print('ROI loss', timer() - a)
        return rpn_cls_loss + rpn_loc_loss + roi_cls_loss + roi_loc_loss

    def train_step(self, img_tensor, img_info):
        self.optimizer.zero_grad()
        loss = self.forward(img_tensor, img_info)
        print(loss.data)
        loss.backward()
        self.optimizer.step()
