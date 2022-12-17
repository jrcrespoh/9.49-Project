#  Numenta Platform for Intelligent Computing (NuPIC)
#  Copyright (C) 2020, Numenta, Inc.  Unless you have an agreement
#  with Numenta, Inc., for a separate license for this software code, the
#  following terms and conditions apply:
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Affero Public License version 3 as
#  published by the Free Software Foundation.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#  See the GNU Affero Public License for more details.
#
#  You should have received a copy of the GNU Affero Public License
#  along with this program.  If not, see http://www.gnu.org/licenses.
#
#  http://numenta.org/licenses/
#

"""
This trains a simple supervised CNN that can be used to output SDR features
(i.e. sparse feature vectors) derived from images such as MNIST; these can subsequently
be used by other classifiers including GridCellNet, or a decoder to reconstruct
the images
Several functions are based on the k-WTA sparse_cnn.ipynb example in
nupic.torch/examples
"""

import os

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torchvision import datasets, transforms
from tqdm import tqdm

from nupic.torch.modules import KWinners2d, rezero_weights, update_boost_strength
from bit import *

seed_val = 1
torch.manual_seed(seed_val)
np.random.seed(seed_val)

# Parameters
TRAIN_NEW_NET = True  # To generate all the SDRs needed for down-stream use in other
TRAIN_NEW_NET = False
# programs, train a network and run this again with TRAIN_NEW_NET=False

# k-WTA parameters
PERCENT_ON = 0.15  # Recommend 0.15
BOOST_STRENGTH = 20.0  # Recommend 20

DATASET = "mnist"  # Options are "mnist" or "fashion_mnist"; note in some cases
DATASET = "cifar"
SCALE = True
# fashion-MNIST may not have full functionality (e.g. normalization, subsequent use of
# SDRs by downstream classifiers)

MODEL = "BaseCNN"
GRID_SIZE = 6
MODEL = "BiT"
OUT_DIM = 128
GRID_SIZE = 5
BLOCK_UNITS = [3, 4, 6, 3]
PRETRAINED = False
PRETRAINED = True
OUT_DIM = 256
FULL = False
FULL = True
OUT_DIM = 2048
WEIGHTS = "R50x1_160.npz"

LEARNING_RATE = 0.01  # Recommend 0.01
MOMENTUM = 0.5  # Recommend 0.5
EPOCHS = 10  # Recommend 10
FIRST_EPOCH_BATCH_SIZE = 4  # Used for optimizing k-WTA
#FIRST_EPOCH_BATCH_SIZE = 1024
TRAIN_BATCH_SIZE = 1024  # Recommend 128
TEST_BATCH_SIZE = 1000
#TEST_BATCH_SIZE = 1024

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print("Using device: " + str(device))
print("Using model: "+MODEL)


def train(model, loader, optimizer, criterion, post_batch_callback=None):
    """
    Train the model using given dataset loader.
    Called on every epoch.
    :param model: pytorch model to be trained
    :type model: torch.nn.Module
    :param loader: dataloader configured for the epoch.
    :type loader: :class:`torch.utils.data.DataLoader`
    :param optimizer: Optimizer object used to train the model.
    :type optimizer: :class:`torch.optim.Optimizer`
    :param criterion: loss function to use
    :type criterion: function
    :param post_batch_callback: function(model) to call after every batch
    :type post_batch_callback: function
    """
    model.train()
    for _batch_idx, (data, target) in tqdm(enumerate(loader),total=len(loader)):
        data, target = data.to(device), target.to(device)
        optimizer.zero_grad()
        output = model(data)
        loss = criterion(output, target)
        loss.backward()
        optimizer.step()
        if post_batch_callback is not None:
            post_batch_callback(model)


def test(model, loader, test_size, criterion, epoch, sdr_output_subset=None):
    """
    Evaluate pre-trained model using given dataset loader.
    Called on every epoch.
    :param model: Pretrained pytorch model
    :type model: torch.nn.Module
    :param loader: dataloader configured for the epoch.
    :type loader: :class:`torch.utils.data.DataLoader`
    :param test_size: size of split used for evaluation. May != len(loader.dataset)
    :param criterion: loss function to use
    :type criterion: function
    :param epoch: the current epoch; can specify as int or e.g. "pre_epoch"
    :param sdr_output_subset: string specifying the data-subset used for generating
    SDRs
    :return: Dict with "accuracy", "loss" and "total_correct"
    """
    model.eval()
    total = len(loader.dataset)
    loss = 0
    total_correct = 0

    # Store data for SDR-based classifiers
    if sdr_output_subset is not None:
        all_sdrs = []
        all_labels = []

    with torch.no_grad():
        for data, target in tqdm(loader,total=len(loader)):

            data, target = data.to(device), target.to(device)
            output = model(data)

            if sdr_output_subset is not None:
                all_sdrs.append(np.array(model.output_sdr(data).cpu()))
                all_labels.append(target)

            loss += criterion(output, target, reduction="sum").item()  # sum up batch
            # loss
            pred = output.argmax(dim=1, keepdim=True)  # get the index of the max
            # log-probability
            total_correct += pred.eq(target.view_as(pred)).sum().item()

        # Track data on duty-cycle in order to optimize sparse representation for SDR
        # output
        duty_cycle = (model.k_winner.duty_cycle[0, :, 0, 0]).cpu().numpy()

    print("\nMean duty cycle : " + str(np.mean(duty_cycle)))
    print("Stdev duty cycle: " + str(np.std(duty_cycle)))
    plt.hist(duty_cycle, bins=20, facecolor="crimson")
    plt.xlabel("Duty Cycle")
    plt.ylabel("Count")
    plt.xlim(0, 0.6)
    plt.ylim(0, 70)
    plt.savefig("duty_cycle_results/duty_cycle_boost_" + str(BOOST_STRENGTH) + "_"
                + str(epoch) + ".png")
    plt.clf()

    if sdr_output_subset is not None:
        #print('sdrs', type(all_sdrs[0]))
        try:
            all_sdrs = np.concatenate([a.cpu() for a in all_sdrs], axis=0)
        except:
            all_sdrs = np.concatenate(all_sdrs, axis=0)
        #print('labels', type(all_labels[0]))
        try:
            all_labels = np.concatenate([a.cpu() for a in all_labels], axis=0)
        except:
            all_labels = np.concatenate(all_labels, axis=0)

        print("Saving generated SDR and label outputs from data sub-section: "
              + sdr_output_subset)
        np.save("python2_htm_docker/docker_dir/training_and_testing_data/" + DATASET
                + "_SDRs_" + sdr_output_subset, all_sdrs)
        np.save("python2_htm_docker/docker_dir/training_and_testing_data/" + DATASET
                + "_labels_" + sdr_output_subset, all_labels)

    return {"accuracy": total_correct / test_size,
            "loss": loss / test_size,
            "total_correct": total_correct}


class SequentialSubSampler(torch.utils.data.Sampler):
    r"""Custom sampler to take elements sequentially from a given list of indices.
    Performing sampling sequentially is helpful for keeping track of the generated SDRs
    and their correspondence to examples in the MNIST data-set.
    Using indices to sub-sample enables creating a splits of the training data.

    Arguments:
        indices (sequence): a sequence of indices
    """

    def __init__(self, indices):
        self.indices = indices

    def __iter__(self):
        return (self.indices[i] for i in range(len(self.indices)))

    def __len__(self):
        return len(self.indices)


def post_batch(model):
    model.apply(rezero_weights)


class SDRCNNBase(nn.Module):
    """
    Classifier that uses k-WTA to create a sparse representation after the
    second pooling operation.
    This sparse operation can subsequently be binarized and output so as to
    generate SDR-like representaitons given an input image.
    """
    def __init__(self, in_channels, percent_on, boost_strength):
        super(SDRCNNBase, self).__init__()
        self.shape = GRID_SIZE
        self.conv1 = nn.Conv2d(in_channels=in_channels, out_channels=64, kernel_size=5,
                               padding=2)
        self.pool1 = nn.MaxPool2d(kernel_size=2, stride=2)
        self.conv2 = nn.Conv2d(in_channels=64, out_channels=128, kernel_size=5,
                               padding=0)
        self.pool2 = nn.MaxPool2d(kernel_size=2, stride=2)
        self.downsample = nn.AdaptiveAvgPool2d(GRID_SIZE)
        self.k_winner = KWinners2d(channels=128, percent_on=percent_on,
                                   boost_strength=boost_strength, local=True)
        self.dense1 = nn.Linear(in_features=128 * self.shape * self.shape, out_features=256)
        self.dense2 = nn.Linear(in_features=256, out_features=128)
        self.output = nn.Linear(in_features=128, out_features=10)
        self.softmax = nn.LogSoftmax(dim=1)

    def until_kwta(self, inputs):
        x = F.relu(self.conv1(inputs))
        x = self.pool1(x)
        x = F.relu(self.conv2(x))
        x = self.pool2(x)
        if DATASET != "mnist": x = self.downsample(x)
        x = self.k_winner(x)
        x = x.view(-1, 128 * self.shape * self.shape)

        return x

    def forward(self, inputs):
        x = self.until_kwta(inputs)
        x = F.relu(self.dense1(x))
        x = F.relu(self.dense2(x))
        x = self.softmax(self.output(x))

        return x

    def output_sdr(self, inputs):
        """Returns a binarized SDR-like output from the CNN"s
        mid-level representations"""
        x = self.until_kwta(inputs)
        x = (x > 0).float()

        return x


class SDRBiT(nn.Module):
    """
    BiT Classifier that uses k-WTA to create a sparse representation after the
    second pooling operation.
    This sparse operation can subsequently be binarized and output so as to
    generate SDR-like representaitons given an input image.
    """
    def __init__(self, in_channels, percent_on, boost_strength):
        super(SDRBiT, self).__init__()
        self.root = nn.Sequential(OrderedDict([
            ('conv', StdConv2d(in_channels, 64, kernel_size=7, stride=2, padding=3, bias=False)),
            ('pad', nn.ConstantPad2d(1, 0)),
            ('pool', nn.MaxPool2d(kernel_size=3, stride=2, padding=0)),
        ]))
        self.body = nn.Sequential(OrderedDict([
            ('block1', nn.Sequential(OrderedDict(
                [('unit01', PreActBottleneck(cin=64, cout=OUT_DIM, cmid=64))] +
                [(f'unit{i:02d}', PreActBottleneck(cin=OUT_DIM, cout=OUT_DIM, cmid=64)) for i in range(2, BLOCK_UNITS[0] + 1)],
            ))),
        ] if not FULL else [
            ('block1', nn.Sequential(OrderedDict(
                [('unit01', PreActBottleneck(cin=64, cout=256, cmid=64))] +
                [(f'unit{i:02d}', PreActBottleneck(cin=256, cout=256, cmid=64)) for i in range(2, BLOCK_UNITS[0] + 1)],
            ))),
            ('block2', nn.Sequential(OrderedDict(
                [('unit01', PreActBottleneck(cin=256, cout=512, cmid=128, stride=2))] +
                [(f'unit{i:02d}', PreActBottleneck(cin=512, cout=512, cmid=128)) for i in range(2, BLOCK_UNITS[1] + 1)],
            ))),
            ('block3', nn.Sequential(OrderedDict(
                [('unit01', PreActBottleneck(cin=512, cout=1024, cmid=256, stride=2))] +
                [(f'unit{i:02d}', PreActBottleneck(cin=1024, cout=1024, cmid=256)) for i in range(2, BLOCK_UNITS[2] + 1)],
            ))),
            ('block4', nn.Sequential(OrderedDict(
                [('unit01', PreActBottleneck(cin=1024, cout=2048, cmid=512, stride=2))] +
                [(f'unit{i:02d}', PreActBottleneck(cin=2048, cout=2048, cmid=512)) for i in range(2, BLOCK_UNITS[3] + 1)],
            ))),
        ]))

        self.shape = GRID_SIZE
        self.downsample = nn.AdaptiveAvgPool2d(self.shape)
        self.k_winner = KWinners2d(channels=OUT_DIM, percent_on=percent_on,
                                   boost_strength=boost_strength, local=True)
        self.dense1 = nn.Linear(in_features=OUT_DIM * self.shape * self.shape, out_features=256)
        self.dense2 = nn.Linear(in_features=256, out_features=128)
        self.output = nn.Linear(in_features=128, out_features=10)
        self.softmax = nn.LogSoftmax(dim=1)
    
    def load_from(self, weights, prefix='resnet/'):
        with torch.no_grad():
            self.root.conv.weight.copy_(tf2th(weights[f'{prefix}root_block/standardized_conv2d/kernel']))
            for bname, block in self.body.named_children():
                for uname, unit in block.named_children():
                    unit.load_from(weights, prefix=f'{prefix}{bname}/{uname}/')

    def until_kwta(self, inputs):
        x = self.root(inputs)
        x = self.body(x)
        x = self.downsample(x)
        x = self.k_winner(x)
        x = x.view(-1, OUT_DIM * self.shape * self.shape)

        return x

    def forward(self, inputs):
        x = self.until_kwta(inputs)
        x = F.relu(self.dense1(x))
        x = F.relu(self.dense2(x))
        x = self.softmax(self.output(x))

        return x

    def output_sdr(self, inputs):
        """Returns a binarized SDR-like output from the CNN"s
        mid-level representations"""
        x = self.until_kwta(inputs)
        x = (x > 0).float()

        return x


def data_setup():
    """
    Note there are three data-sets used in all of the experiments; the training
    and testing data used for the CNN (and the decoder later), which can be thought
    of as a cross-validation split of the MNIST training data-set, and the true test
    data of MNIST.
    The true MNIST test data-set (which is saved as "SDR_classifiers_testing" data
    later) is used to evaluate the classifiers that receive SDRrs as input - which
    are trained on the "test" data-set for the CNN/decoder ("test_CNN" data - which
    is therefore saved as "SDR_classifiers_training" data later)
    Hyper-parameter tuning of the CNN/decoder should be performed using the results
    from the "test_CNN" data.
    """
    print("Loading data-sets")

    if DATASET == "mnist":
        print("Using MNIST data-set")
        train_dataset = datasets.MNIST(
            "data",
            train=True,
            download=True,
            transform=transforms.Compose(
                ([] if not SCALE else [transforms.Resize((160, 160)),
                                           transforms.RandomCrop((128, 128)),
                                           transforms.RandomHorizontalFlip(),]) +
                [transforms.ToTensor(),
                transforms.Normalize((0.1307,), (0.3081,))] +
                ([] if not PRETRAINED else [transforms.Lambda(
                    lambda x: x.repeat(3,1,1)
                )])
            )
        )
        test_dataset = datasets.MNIST(
            "data",
            train=False,
            download=True,
            transform=transforms.Compose(
                ([] if not SCALE else [transforms.Resize((128, 128)),]) +
                [transforms.ToTensor(),
                transforms.Normalize((0.1307,), (0.3081,))] +
                ([] if not PRETRAINED else [transforms.Lambda(
                    lambda x: x.repeat(3,1,1)
                )])
            )
        )

    elif DATASET == "fashion_mnist":
        print("Using Fashion-MNIST data-set")
        normalize = transforms.ToTensor()  # TODO normalization of pixel intensities
        train_dataset = datasets.FashionMNIST("data", train=True, download=True,
                                              transform=normalize)
        test_dataset = datasets.FashionMNIST("data", train=False,
                                             download=True,
                                             transform=normalize)

    elif DATASET == "cifar":
        print("Using CIFAR10 data-set")
        
        train_dataset = datasets.CIFAR10(
            "data",
            train=True,
            download=True,
            transform=transforms.Compose(
                ([] if not SCALE else [transforms.Resize((160, 160)),
                                           transforms.RandomCrop((128, 128)),
                                           transforms.RandomHorizontalFlip(),]) +
                [transforms.ToTensor(),
                 transforms.Normalize((0.4914, 0.4822, 0.4465),(0.247, 0.243, 0.261))]
            )
        )

        test_dataset = datasets.CIFAR10(
            "data",
            train=False,
            download=True,
            transform=transforms.Compose(
                ([] if not SCALE else [transforms.Resize((128, 128)),]) +
                [transforms.ToTensor(),
                 transforms.Normalize((0.4914, 0.4822, 0.4465),(0.247, 0.243, 0.261))]
            )
        )


    total_traing_len = len(train_dataset)
    indices = range(total_traing_len)
    val_split = int(np.floor(0.1 * total_traing_len))
    train_idx, test_cnn_idx = indices[val_split:], indices[:val_split]

    train_sampler = SequentialSubSampler(train_idx)
    test_cnn_sampler = SequentialSubSampler(test_cnn_idx)
    test_sdr_class_len = len(test_dataset)
    test_sdr_classifier_sample = SequentialSubSampler(range(test_sdr_class_len))

    # Configure data loaders
    first_loader = torch.utils.data.DataLoader(train_dataset,
                                               batch_size=FIRST_EPOCH_BATCH_SIZE,
                                               sampler=train_sampler)
    train_loader = torch.utils.data.DataLoader(train_dataset,
                                               batch_size=TRAIN_BATCH_SIZE,
                                               sampler=train_sampler)
    test_cnn_loader = torch.utils.data.DataLoader(train_dataset,
                                                  batch_size=TEST_BATCH_SIZE,
                                                  sampler=test_cnn_sampler)
    test_sdrc_loader = torch.utils.data.DataLoader(test_dataset,
                                                   batch_size=TEST_BATCH_SIZE,
                                                   sampler=test_sdr_classifier_sample)

    return (first_loader, train_loader, test_cnn_loader, test_sdrc_loader,
            len(train_idx), len(test_cnn_idx), test_sdr_class_len)


if __name__ == "__main__":

    (first_loader, train_loader, test_cnn_loader, test_sdrc_loader, training_len,
        testing_cnn_len, testing_sdr_classifier_len) = data_setup()

    in_channels = 3 if DATASET == 'cifar' or PRETRAINED else 1 
    cnn = SDRCNNBase if MODEL != "BiT" else SDRBiT
    sdr_cnn = cnn(in_channels=in_channels, percent_on=PERCENT_ON, boost_strength=BOOST_STRENGTH)
    if PRETRAINED:
        print("Loading weights from "+WEIGHTS)
        sdr_cnn.load_from(np.load(WEIGHTS))
    sdr_cnn.to(device)

    if os.path.exists("saved_networks/") is False:
        try:
            os.mkdir("saved_networks/")
        except OSError:
            pass

    if os.path.exists("duty_cycle_results/") is False:
        try:
            os.mkdir("duty_cycle_results/")
        except OSError:
            pass

    if (os.path.exists("python2_htm_docker/docker_dir/training_and_testing_data/")
            is False):
        try:
            os.mkdir("python2_htm_docker/docker_dir/training_and_testing_data/")
        except OSError:
            pass

    if TRAIN_NEW_NET is True:

        print("Performing first epoch for update-boost-strength")
        sgd = optim.SGD(sdr_cnn.parameters(), lr=LEARNING_RATE, momentum=MOMENTUM)
        train(model=sdr_cnn, loader=first_loader, optimizer=sgd, criterion=F.nll_loss,
              post_batch_callback=post_batch)

        sdr_cnn.apply(update_boost_strength)

        test(model=sdr_cnn, loader=test_cnn_loader, test_size=testing_cnn_len,
             epoch="pre_epoch", criterion=F.nll_loss)

        print("Performing full training")
        for epoch in tqdm(range(1, EPOCHS),total=EPOCHS):
            train(model=sdr_cnn, loader=train_loader, optimizer=sgd,
                  criterion=F.nll_loss, post_batch_callback=post_batch)
            sdr_cnn.apply(update_boost_strength)
            results = test(model=sdr_cnn, loader=test_cnn_loader,
                           test_size=testing_cnn_len, epoch=epoch,
                           criterion=F.nll_loss)
            print(results)

        print("Saving network state...")
        torch.save(sdr_cnn.state_dict(), "saved_networks/sdr_cnn.pt")

    elif MODEL != "BiT":
        print("Evaluating a pre-trained model:")
        sdr_cnn.load_state_dict(torch.load("saved_networks/sdr_cnn.pt"))

    print("\nSaving SDR-representations for later use.")
    print("\nResults from training data-set. The output SDRs are saved as "
          "base_net_training, and are used later to train the decoder")
    results = test(model=sdr_cnn, loader=train_loader, test_size=training_len,
                   epoch="final_train", criterion=F.nll_loss,
                   sdr_output_subset="base_net_training")
    print(results)
    print("\nResults from data-set for evaluating CNN/decoder. The output SDRs are "
          "saved as SDR_classifiers_training")
    results = test(model=sdr_cnn, loader=test_cnn_loader, test_size=testing_cnn_len,
                   epoch="test_CNN", criterion=F.nll_loss,
                   sdr_output_subset="SDR_classifiers_training")
    print(results)
    print("\nResults from data-set for evaluating later SDR-based classifiers. The "
          "output SDRs are saved as SDR_classifiers_testing")
    results = test(model=sdr_cnn, loader=test_sdrc_loader,
                   test_size=testing_sdr_classifier_len,
                   epoch="test_SDRs", criterion=F.nll_loss,
                   sdr_output_subset="SDR_classifiers_testing")
    print(results)
