import torch.nn as nn
import torch
from torch.autograd import Variable
import torchvision.models as models
import numpy as np

from generate_base_anchors import generate_base_anchors, anchor_proposals
from box_parametrize import box_deparameterize_gpu
from roi_module import RoIPooling2D
from faster_rcnn import FasterRCNN
from non_maximum_suppression import non_maximum_suppression_rpn


def load_vgg16():
    model = models.vgg16(pretrained=True)
    features = list(model.features)[:30]
    features = nn.Sequential(*features)
    classifier = list(model.classifier)[:3]
    classifier = nn.Sequential(*classifier)
    # requires_grad = False for the first few layers
    for i in range(10):
        for param in features[i].parameters():
            param.requires_grad = False

    return features, classifier


def initialize_params(x, mean=0, stddev=0.01):
    x.weight.data.normal_(mean, stddev)
    x.bias.data.zero_()


def create_rpn_proposals(locs, scores, anchors, img_size):
    """
    create rpn proposal based on rpn result
    :param locs: (N, 4), pytorch tensor of RPN prediction
    :param scores: (N, ), pytorch tensor of RPN prediction
    :param anchors: (N, 4), pytorch tensor
    :param img_size: [height, width]
    :return: [K, 4], pytorch tensor, rpn proposals
    """
    nms_thresh = 0.7
    num_pre_nms = 12000
    num_post_nms = 2000
    min_size = 16
    img_h = img_size[0]
    img_w = img_size[1]

    rois = box_deparameterize_gpu(locs, anchors)

    # take top num_pre_nms rois and scores

    _, order = torch.sort(scores, descending=True)
    order = order[:num_pre_nms]
    rois = rois[order, :].contiguous()
    scores = scores[order].contiguous()


    # clip bbox to image size
    rois[rois < 0] = 0
    rois[:, 0][rois[:, 0] > img_h] = img_h
    rois[:, 1][rois[:, 1] > img_w] = img_w
    rois[:, 2][rois[:, 2] > img_h] = img_h
    rois[:, 3][rois[:, 3] > img_w] = img_w

    # remove boxes with size smaller than threshold
    height = rois[:, 2] - rois[:, 0]
    width = rois[:, 3] - rois[:, 1]
    keep = torch.nonzero((height >= min_size) & (width >= min_size))[:, 0]
    rois = rois[keep, :].contiguous()
    scores = scores[keep].contiguous()

    #nms
    _, roi_selected = non_maximum_suppression_rpn(rois, nms_thresh, scores, num_post_nms)

    return roi_selected


class FasterRCNNVGG16(FasterRCNN):
    def __init__(self, num_class=21, ratios=[0.5, 1., 2.], scales=[8, 16, 32], stride=16):
        # load pre-trained model
        feature_extractor, classifier = load_vgg16()
        feature_extractor = feature_extractor.cuda()
        classifier = classifier.cuda()
        rpn = RPN(512, 512, ratios, scales, stride).cuda()
        head = VGG16ROIHead(num_class,
                            roi_size=[7, 7],
                            spatial_scale=1./stride,
                            classifier=classifier).cuda()

        super(FasterRCNNVGG16, self).__init__(feature_extractor, rpn, head)


class RPN(nn.Module):
    def __init__(self, in_channel, out_channel, ratios, scales, stride):
        super(RPN, self).__init__()
        self.stride = stride
        self.scales = scales
        self.ratios = ratios
        self.anchor_base = generate_base_anchors(stride, ratios, scales)
        self.num_anchor_base = self.anchor_base.shape[0]
        self.all_anchors = anchor_proposals(64, 64, stride, self.anchor_base)
        self.leaky_relu = nn.LeakyReLU(0.01)
        self.conv = nn.Conv2d(in_channel, out_channel, 3, stride=1, padding=1)
        self.score = nn.Conv2d(out_channel, self.num_anchor_base * 2, 1, stride=1, padding=0)
        self.loc = nn.Conv2d(out_channel, self.num_anchor_base * 4, 1, stride=1, padding=0)
        self.softmax = nn.Softmax(dim=1)
        initialize_params(self.conv, 0, 0.01)
        initialize_params(self.score, 0, 0.01)
        initialize_params(self.loc, 0, 0.01)

    def forward(self, x, img_size):
        """
        forward function of RPN
        :param x: extracted features from image tensor, pytroch Variable
        :param img_size: [H, W]
        :param all_anchors: anchors generate at the beginning of training/testing reusable for each iteration
        :return: torch Variable: rpn_locs, (N, 4)
                                 rpn_scores, (N, 2)
                torch tensors: rois, (K, 4)
                         anchors (L, 4)
        """
        n, _, feature_h, feature_w = x.size()
        if n != 1:
            raise ValueError('Currently only batch size 1 is supported.')
        anchors = self.all_anchors[0:feature_h, 0:feature_w].contiguous().view(-1, 4)
        x = self.leaky_relu(self.conv(x))

        rpn_locs = self.loc(x)
        rpn_locs = rpn_locs.permute(0, 2, 3, 1).contiguous().view(-1, 4)

        rpn_scores = self.score(x)
        rpn_scores = rpn_scores.permute(0, 2, 3, 1).contiguous().view(-1, 2)
        rpn_scores_after_softmax = self.softmax(rpn_scores)
        rpn_fg_scores = rpn_scores_after_softmax[:, 1].contiguous()
        from timeit import default_timer as timer
        a = timer()
        rois = create_rpn_proposals(rpn_locs.data, rpn_fg_scores.data, anchors, img_size)
        print('create_rpn_proposals', timer()-a)

        return rpn_locs, rpn_scores, rois, anchors


class VGG16ROIHead(nn.Module):
    def __init__(self, num_class, roi_size, spatial_scale, classifier):
        super(VGG16ROIHead, self).__init__()
        self.classifier = classifier
        self.cls_loc = nn.Linear(4096, num_class * 4)
        self.score = nn.Linear(4096, num_class)
        initialize_params(self.cls_loc, 0, 0.01)
        initialize_params(self.score, 0, 0.01)

        self.num_class = num_class
        self.roi_size = roi_size
        self.spatial_scale = spatial_scale
        self.roi_pooling = RoIPooling2D(self.roi_size[0],
                                        self.roi_size[1],
                                        self.spatial_scale)

    def forward(self, x, rois):
        roi_indices = torch.zeros(rois.size()[0]).float().cuda()
        indices_and_rois = torch.stack([roi_indices, rois[:, 0], rois[:, 1], rois[:, 2], rois[:, 3]], dim=1)
        xy_indices_and_rois = Variable(indices_and_rois[:, [0, 2, 1, 4, 3]]).contiguous()

        pool_result = self.roi_pooling(x, xy_indices_and_rois)
        pool_result = pool_result.view(pool_result.size()[0], -1)
        fc = self.classifier(pool_result)
        roi_cls_locs = self.cls_loc(fc)
        roi_scores = self.score(fc)

        return roi_cls_locs, roi_scores


def test():
    # test create_rpn_proposals()
    locs = Variable(torch.zeros((6, 4))).float().cuda()
    scores = Variable(torch.FloatTensor([1, 0.1, 0.2, 0.3, 0.4, 0.5])).cuda()
    anchors = np.array([[0, 0, 17, 17],
                        [0, 0, 100, 100],
                        [0, 0, 110, 110],
                        [0, 0, 120, 120],
                        [200, 200, 300, 300],
                        [200, 200, 255, 255]])
    img_size = [260, 260]
    roi = create_rpn_proposals(locs, scores, anchors, img_size)
    print(roi)

    # test anchor_proposals()
    anchor_base = generate_base_anchors(16, [0.5, 1.0, 2.0], [8, 16, 32])
    anchors = anchor_proposals(16, 16, 16, anchor_base)

    center = (anchors[:, [0, 1]] + anchors[:, [2, 3]])/2
    print(center[[9 * i for i in range(16 * 16)], :]/16)

    # test load_vgg16
    feature_extractor, classifier = load_vgg16()
    print(feature_extractor, classifier)
    for param in feature_extractor.parameters():
        print(param.requires_grad)
    for param in classifier.parameters():
        print(param.requires_grad)

    # test FasterRCNNVGG16()
    fast_rcnn = FasterRCNNVGG16().cuda()
    print(fast_rcnn.rpn, fast_rcnn.head)

    # load image
    img_dict = np.load('../VOCdevkit/img_box_dict.npy')[()]
    from rescale_image import rescale_image
    from torchvision import transforms
    for img_dir, img_info in img_dict.items():
        img, img_info = rescale_image(img_dir, img_info)
        img_size = img_info['img_size']
        normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
        img_tensor = Variable(normalize(torch.from_numpy(np.transpose(img / 255, (2, 0, 1))))).float()
        img_tensor = torch.unsqueeze(img_tensor, 0).cuda()
        feature = fast_rcnn.extractor(img_tensor)
        rpn_locs, rpn_scores, rois, anchors = fast_rcnn.rpn(feature, img_size)
        roi_cls_locs, roi_scores = fast_rcnn.head(feature, rois)
        print(rpn_locs, rpn_scores, rois, anchors)
        print(roi_cls_locs, roi_scores)


if __name__ == "__main__":
    test()
