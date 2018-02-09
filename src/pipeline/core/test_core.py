import os
import glob
import re
import csv

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torchvision import transforms
from scipy.stats import gmean
from PIL import Image
from tqdm import tqdm

from . import utils
from . import train_utils
from . import mytransforms
from torch.utils.data import Dataset
from torch.utils.data import DataLoader


class OpenCVCropBase(object):
    def __init__(self, size):
        self._size = size

    def __call__(self, img):
        i, j = self.get_params(img)
        return img[i: i + self._size, j: j + self._size]


class OpenCVCenterCrop(OpenCVCropBase):
    def __init__(self, size):
        super().__init__(size)

    def get_params(self, img):
        h, w, c = img.shape
        if h == self._size and w == self._size:
            return 0, 0
        th = tw = self._size
        i = int(round((h - th) / 2.))
        j = int(round((w - tw) / 2.))
        return i, j


def _five_crop(img, size):
    h, w, c = img.shape
    assert c == 3, 'Something wrong with channels order'
    if size > w or size > h:
        raise ValueError(
            "Requested crop size {} is bigger than input size {}".format(size, (h, w)))
    tl = img[0: size, 0: size]
    tr = img[0: size, w - size: w]
    bl = img[h - size: h, 0: size]
    br = img[h - size: h, w - size: w]
    center = OpenCVCenterCrop(size)(img)
    return tl, tr, bl, br, center


def _get_res50_feats(df, path):
    probs = list(np.array(
        df.loc[df['fname'] == path].drop('fname', axis=1),
        dtype=np.float32).flatten())
    return probs


class LastValDataset(Dataset):
    def __init__(self, ids):
        self._ids = ids

    def __len__(self):
        return len(self._ids)

    def __getitem__(self, item):
        idx = self._ids[item]
        img = np.array(Image.open(idx))
        assert img.shape == (512, 512, 3), img.shape
        assert len(img.shape) == 3, img.shape

        original_img = img

        original_manipulated = np.float32([1. if idx.find('manip') != -1 else 0.])

        batch_size = 5 * 8
        img_batch = np.zeros((batch_size, 480, 480, 3), dtype=np.float32)
        manipulated_batch = np.zeros((batch_size, 1), dtype=np.float32)

        i = 0
        img = np.copy(original_img)
        manipulated = np.copy(original_manipulated)
        five_crops = _five_crop(img, 480)
        for crop in five_crops:
            d4s_on_crop = mytransforms._full_d4(crop)
            for d4_crop in d4s_on_crop:
                img_batch[i] = d4_crop.copy().astype(np.float32)
                manipulated_batch[i] = manipulated
                i += 1
        return img_batch, manipulated_batch, idx


def predict_on_test(model, weights_path, test_folder, use_tta, ensembling, crop_size):
    print('using D4 test_core')
    if use_tta:
        print('Predicting with TTA10: five crops + orientation flip')
    else:
        print('Prediction without TTA')
    ids = glob.glob(os.path.join(test_folder, '*.tif'))

    ids.sort()
    dataset = LastValDataset(ids)
    loader = DataLoader(dataset, batch_size=1, num_workers=7, pin_memory=True)

    match = re.search(r'([^/]*)\.pth', weights_path)
    model_name = match.group(1) + ('_tta_' + ensembling if use_tta else '')
    csv_name = 'submit/submission_' + model_name + '.csv'

    model.eval()
    with open(csv_name, 'w') as csvfile:

        csv_writer = csv.writer(csvfile, delimiter=',', quotechar='|', quoting=csv.QUOTE_MINIMAL)
        csv_writer.writerow(['fname', 'camera'])
        classes = []
        preds = None
        names = []
        tta_preds = None
        tta_names = []

        for img_batch_tensor, manipulated_batch_tensor, idx_tensor in tqdm(loader):
            img_batch = img_batch_tensor.view(40, 480, 480, 3).numpy()
            manipulated_batch = manipulated_batch_tensor.view(40, 1).numpy()
            idx = str(idx_tensor[0])
            img_batch, manipulated_batch = train_utils.variable(torch.from_numpy(img_batch), True), \
                                           train_utils.variable(torch.from_numpy(manipulated_batch), True)
            prediction = nn.functional.softmax(model(img_batch, manipulated_batch)).data.cpu().numpy()
            tta_names += [idx.split('/')[-1] + '_tta{}'.format(i) for i in range(prediction.shape[0])]
            tta_preds = np.vstack([tta_preds, prediction]) if preds is not None else prediction
            if prediction.shape[0] != 1:  # TTA
                if ensembling == 'geometric':
                    prediction = gmean(prediction, axis=0)
                else:
                    raise NotImplementedError()

            prediction_class_idx = np.argmax(prediction)

            csv_writer.writerow([idx.split('/')[-1], utils.CLASSES[prediction_class_idx]])
            classes.append(prediction_class_idx)
            names.append(idx.split('/')[-1])
            preds = np.vstack([preds, prediction]) \
                if preds is not None else prediction

        df_data = np.append(preds, np.array(names, copy=False, subok=True, ndmin=2).T, axis=1)
        df = pd.DataFrame(data=df_data, columns=utils.CLASSES + ['fname'])
        os.makedirs('submit', exist_ok=True)
        df.to_hdf('submit/{}_test_pr.h5'.format(model_name), 'prob')

        tta_data = np.append(tta_preds, np.array(tta_names, copy=False, subok=True, ndmin=2).T, axis=1)
        tta_df = pd.DataFrame(data=tta_data, columns=utils.CLASSES + ['fname'])
        tta_df.to_hdf('submit/{}_test_pr_with_tta.h5'.format(model_name), 'prob')

        print("Test set predictions distribution:")
        utils.print_distribution(None, classes=classes)
        print("Now you are ready to:")
        print("kg submit {}".format(csv_name))
