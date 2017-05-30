#!/usr/bin/env python
from __future__ import print_function, division

import os
import time
import numpy as np
import theano
import theano.tensor as T
import lasagne
import argparse
import matplotlib.pyplot as plt

from os.path import join
from scipy.io import loadmat

from utils import compressed_sensing as cs
from utils.metric import complex_psnr

from cascadenet.network.model import build_d2_c2
from cascadenet.util.helpers import from_lasagne_format
from cascadenet.util.helpers import to_lasagne_format


def prep_input(im, gauss_ivar=1e-3):
    """Undersample the batch, then reformat them into what the network accepts.

    Parameters
    ----------
    gauss_ivar: float - controls the undersampling rate. higher the value, more undersampling 
    """
    mask = cs.cartesian_mask(im.shape, gauss_ivar,
                             centred=False,
                             sample_high_freq=True,
                             sample_centre=True,
                             sample_n=8)
    # mask = cs.one_line(im.shape)
    im_und, k_und = cs.undersample(im, mask, centred=False, norm='ortho')

    im_gnd_l = to_lasagne_format(im)
    im_und_l = to_lasagne_format(im_und)
    k_und_l = to_lasagne_format(k_und)
    mask_l = to_lasagne_format(mask, mask=True)

    return im_und_l, k_und_l, mask_l, im_gnd_l


def iterate_minibatch(data, batch_size, shuffle=True):
    n = len(data)

    if shuffle:
        data = np.random.permutation(data)

    for i in xrange(0, n, batch_size):
        yield data[i:i+batch_size]


def create_dummy_data():
    """
    Creates dummy dataset from one knee subject for demo.
    In practice, one should take much bigger dataset,
    as well as train & test should have similar distribution.
    
    Source: http://mridata.org/
    """
    data = loadmat(join(project_root, './data/lustig_knee_p2.mat'))['xn']
    nx, ny, nz, nc = data.shape

    train = np.transpose(data, (3, 0, 1, 2)).reshape((-1, ny, nz))
    validate = np.transpose(data, (3, 1, 0, 2)).reshape((-1, nx, nz))
    test = np.transpose(data, (3, 2, 0, 1)).reshape((-1, nx, ny))

    return train, validate, test


def compile_fn(network, net_config, args):
    """
    Create Training function and validation function
    """
    # Hyper-parameters
    base_lr = float(args.lr[0])
    l2 = float(args.l2[0])

    # Theano variables
    input_var = net_config['input'].input_var
    mask_var = net_config['mask'].input_var
    kspace_var = net_config['kspace_input'].input_var
    target_var = T.tensor4('targets')

    # Objective
    pred = lasagne.layers.get_output(network)
    # complex valued signal has 2 channels, which counts as 1.
    loss_sq = lasagne.objectives.squared_error(target_var, pred).mean() * 2
    if l2:
        l2_penalty = lasagne.regularization.regularize_network_params(network,
                                                                      lasagne.regularization.l2)
        loss = loss_sq + l2_penalty * l2

    update_rule = lasagne.updates.adam
    params = lasagne.layers.get_all_params(network, trainable=True)
    updates = update_rule(loss, params, learning_rate=base_lr)

    print(' Compiling ... ')
    t_start = time.time()
    train_fn = theano.function([input_var, mask_var, kspace_var, target_var],
                               [loss], updates=updates,
                               on_unused_input='ignore')

    val_fn = theano.function([input_var, mask_var, kspace_var, target_var],
                             [loss, pred],
                             on_unused_input='ignore')
    t_end = time.time()
    print(' ... Done, took %.4f s' % (t_end - t_start))

    return train_fn, val_fn


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--num_epoch', metavar='int', nargs=1, default=['10'],
                        help='number of epochs')
    parser.add_argument('--batch_size', metavar='int', nargs=1, default=['10'],
                        help='batch size')
    parser.add_argument('--lr', metavar='float', nargs=1,
                        default=['0.001'], help='initial learning rate')
    parser.add_argument('--l2', metavar='float', nargs=1,
                        default=['1e-6'], help='l2 regularisation')
    parser.add_argument('--gauss_ivar', metavar='float', nargs=1,
                        default=['0.0015'],
                        help='Sensitivity for Gaussian Distribution which decides the undersampling rate of the Cartesian mask')
    parser.add_argument('--savefig', action='store_true', help='Save output images and masks')

    args = parser.parse_args()

    # Project config
    model_name = 'd2_c2'
    gauss_ivar = float(args.gauss_ivar[0])  # undersampling rate
    num_epoch = int(args.num_epoch[0])
    batch_size = int(args.batch_size[0])
    Nx, Ny = 128, 128
    save_fig = args.savefig
    save_every = 5

    # Configure directory info
    project_root = '.'
    save_dir = join(project_root, 'models/%s' % model_name)
    if not os.path.isdir(save_dir):
        os.makedirs(save_dir)

    # Specify network
    input_shape = (batch_size, 2, Nx, Ny)
    net_config, net,  = build_d2_c2(input_shape)

    # Compute acceleration rate
    dummy_mask = cs.cartesian_mask((100, Nx, Ny), gauss_ivar,
                                   sample_high_freq=True,
                                   sample_centre=True, sample_n=8)
    acc = dummy_mask.size / np.sum(dummy_mask)
    print('Acceleration Rate: {:.2f}'.format(acc))

    # Compile function
    train_fn, val_fn = compile_fn(net, net_config, args)

    # Create Dataset
    train, validate, test = create_dummy_data()

    for epoch in xrange(num_epoch):
        t_start = time.time()
        # Training
        train_err = 0
        train_batches = 0
        for im in iterate_minibatch(train, batch_size, shuffle=True):
            im_und, k_und, mask, im_gnd = prep_input(im, gauss_ivar=gauss_ivar)
            err = train_fn(im_und, mask, k_und, im_gnd)[0]
            train_err += err
            train_batches += 1

            # if train_batches == 100:
            #     break

        validate_err = 0
        validate_batches = 0
        for im in iterate_minibatch(validate, batch_size, shuffle=False):
            im_und, k_und, mask, im_gnd = prep_input(im, gauss_ivar=gauss_ivar)
            err, pred = val_fn(im_und, mask, k_und, im_gnd)
            validate_err += err
            validate_batches += 1

            # if validate_batches == 10:
            #     break

        vis = []
        test_err = 0
        base_psnr = 0
        test_psnr = 0
        test_batches = 0
        for im in iterate_minibatch(test, batch_size, shuffle=False):
            im_und, k_und, mask, im_gnd = prep_input(im, gauss_ivar=gauss_ivar)

            err, pred = val_fn(im_und, mask, k_und, im_gnd)
            test_err += err
            for im_i, und_i, pred_i in zip(im,
                                           from_lasagne_format(im_und),
                                           from_lasagne_format(pred)):
                base_psnr += complex_psnr(im_i, und_i, peak='max')
                test_psnr += complex_psnr(im_i, pred_i, peak='max')
            test_batches += 1

            if save_fig and test_batches % save_every == 0:
                vis.append((im[0],
                            from_lasagne_format(pred)[0],
                            from_lasagne_format(im_und)[0],
                            from_lasagne_format(mask, mask=True)[0]))

            # if test_batches == 100:
            #     break

        t_end = time.time()

        # Then we print the results for this epoch:
        print("Epoch {}/{}".format(epoch+1, num_epoch+1))
        print(" time: {}s".format(t_end - t_start))
        print(" training loss:\t\t{:.6f}".format(train_err / max(1, train_batches)))
        print(" validation loss:\t{:.6f}".format(validate_err / validate_batches))
        print(" test loss:\t\t{:.6f}".format(test_err / test_batches))
        print(" base PSNR:\t\t{:.6f}".format(base_psnr / (test_batches*batch_size)))
        print(" test PSNR:\t\t{:.6f}".format(test_psnr / (test_batches*batch_size)))

        # save the model
        if save_fig and (epoch % save_every == 0 or epoch in [1, 2, num_epoch-1]):
            i = 0
            for im_i, pred_i, und_i, mask_i in vis:
                plt.imsave(join(save_dir, 'im{0}.png'.format(i)),
                           abs(np.concatenate([und_i, pred_i, im_i, im_i - pred_i], 1)), cmap='gray')
                plt.imsave(join(save_dir, 'mask{0}.png'.format(i)), mask_i, cmap='gray')
                i += 1

            name = '%s_epoch_%d.npz' % (model_name, epoch)
            np.savez(join(save_dir, name),
                     *lasagne.layers.get_all_param_values(net))
            print('model parameters saved at %s' % join(os.getcwd(), name))
            print('')