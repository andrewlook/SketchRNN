import json
import math
import os
from pathlib import Path
from pprint import pprint
from typing import Any

import numpy as np
import torch
import torch.nn as nn
import wandb
from fastprogress.fastprogress import master_bar, progress_bar
from PIL import Image
from torch import optim
from torch.utils.data import DataLoader

from .dataset import StrokesDataset
from .model import DecoderRNN, EncoderRNN, KLDivLoss, ReconstructionLoss
from .sampler import Sampler


class HParams():
    architecture = 'Pytorch-SketchRNN'

    dataset_source: str = 'look'
    dataset_name: str = 'look_i16__minn10_epsilon1'

    # duration of training run
    epochs = 50000
    # how often to compute validation metrics / persist / sample
    save_every_n_epochs = 100
    # validate_every_n_epochs = 2

    # adaptive learning rate
    lr = 1e-3
    use_lr_decay = False
    min_lr = 1e-5
    lr_decay = 0.9999
    
    # Encoder and decoder sizes
    enc_hidden_size = 256
    dec_hidden_size = 512

    # Batch size
    batch_size = 100

    # Number of features in $z$
    d_z = 128
    # Number of distributions in the mixture, $M$
    n_distributions = 20

    # Weight of KL divergence loss, $w_{KL}$
    kl_div_loss_weight = 0.5
    # decaying weight of KL loss
    use_eta = False
    eta_min = 1e-2
    eta_R = 0.99995

    # Gradient clipping
    grad_clip = 1.
    # Temperature $\tau$ for sampling
    temperature = 0.4

    # Filter out stroke sequences longer than $200$
    max_seq_length = 200

    def __dict__(self):
        return {k: getattr(self, k) for k in self.__dir__() if not k.startswith('__')}


def lr_decay(optimizer, min_lr, lr_decay):
    """Decay learning rate by a factor of lr_decay"""
    for param_group in optimizer.param_groups:
        if param_group['lr'] > min_lr:
            param_group['lr'] *= lr_decay
    return optimizer


class Trainer():
    # Device configurations to pick the device to run the experiment
    device: str
    
    encoder: EncoderRNN
    decoder: DecoderRNN
    optimizer: optim.Adam
    sampler: Sampler

    train_loader: DataLoader
    valid_loader: DataLoader
    train_dataset: StrokesDataset
    valid_dataset: StrokesDataset

    kl_div_loss = KLDivLoss()
    reconstruction_loss = ReconstructionLoss()

    learning_rate: float

    def __init__(self,
                 hp: HParams,
                 device="cuda",
                 models_dir="models",
                 use_wandb=False,
                 wandb_project='sketchrnn-pytorch',
                 wandb_entity='andrewlook'):
        self.hp = hp
        self.device = device
        self.use_wandb = use_wandb
        
        # create a unique run ID, to distinguish saved model checkpoints / sample images
        self.run_id = f"{math.floor(np.random.rand() * 1e6):07d}"
        if self.use_wandb:
            run = wandb.init(
                project=wandb_project,
                entity=wandb_entity,
                config=hp.__dict__(),
            )
            # use wandb's run ID, if available, so checkpoints match W&B's dashboard ID
            self.run_id = run.id

        print('='*60)
        print(f"RUN_ID: {self.run_id}\n")
        print(f"HYPERPARAMETERS:\n")
        print(json.dumps(hp.__dict__(), indent=2))
        print('='*60 + '\n\n')

        self.models_dir = Path(models_dir)
        self.run_dir = self.models_dir / self.run_id
        if not os.path.isdir(self.run_dir):
            os.makedirs(self.run_dir)

        # Initialize step count, to be updated in the training loop
        self.total_steps = 0
        
        # Initialize encoder & decoder
        self.encoder = EncoderRNN(self.hp.d_z, self.hp.enc_hidden_size).to(self.device)
        self.decoder = DecoderRNN(self.hp.d_z, self.hp.dec_hidden_size, self.hp.n_distributions).to(self.device)
        if self.use_wandb:
            wandb.watch((self.encoder, self.decoder), log="all", log_freq=10, log_graph=True)

        # store learning rate as state, so it can be modified by LR decay
        self.learning_rate = self.hp.lr
        self.encoder_optimizer = optim.Adam(self.encoder.parameters(), self.learning_rate)
        self.decoder_optimizer = optim.Adam(self.decoder.parameters(), self.learning_rate)

        self.eta_step = self.hp.eta_min if self.hp.use_eta else 1

        # `npz` file path is `data/quickdraw/[DATASET NAME].npz`
        base_path = Path(f"data/{self.hp.dataset_source}")
        path = base_path / f'{self.hp.dataset_name}.npz'
        # Load the numpy file
        dataset = np.load(str(path), encoding='latin1', allow_pickle=True)

        # Create training dataset
        self.train_dataset = StrokesDataset(dataset['train'], self.hp.max_seq_length)
        # Create validation dataset
        self.valid_dataset = StrokesDataset(dataset['valid'], self.hp.max_seq_length, self.train_dataset.scale)

        # Create training data loader
        self.train_loader = DataLoader(self.train_dataset, self.hp.batch_size, shuffle=True)
        # Create validation data loader
        self.valid_loader = DataLoader(self.valid_dataset, self.hp.batch_size)

        # Create sampler
        self.sampler = Sampler(self.encoder, self.decoder)
        # Pick 5 indices from the validation dataset, so the sampling can be compared across epochs
        self.valid_idxs = [np.random.choice(len(self.valid_dataset)) for _ in range(5)]

    def save(self, epoch):
        torch.save(self.encoder.state_dict(), \
            Path(self.run_dir) / f'runid-{self.run_id}_epoch-{epoch:05d}_encoderRNN.pth')
        torch.save(self.decoder.state_dict(), \
            Path(self.run_dir) / f'runid-{self.run_id}_epoch-{epoch:05d}_decoderRNN.pth')

    def load(self, epoch):
        saved_encoder = torch.load(Path(self.run_dir) / f'runid-{self.run_id}_epoch-{epoch:05d}_encoderRNN.pth')
        saved_decoder = torch.load(Path(self.run_dir) / f'runid-{self.run_id}_epoch-{epoch:05d}_decoderRNN.pth')
        self.encoder.load_state_dict(saved_encoder)
        self.decoder.load_state_dict(saved_decoder)
    
    def log(self, metrics):
        if self.use_wandb:
            wandb.log(metrics, step=self.total_steps)
        else:
            pass
            #pprint({'step': self.total_steps, **metrics})

    def sample(self, epoch, display=False):
        orig_paths = []
        decoded_paths = []
        for idx in self.valid_idxs:
            orig_path = self.run_dir / f'runid-{self.run_id}_epoch-{epoch:05d}_sample-{idx:04d}_orig.png'
            decoded_path = self.run_dir / f'runid-{self.run_id}_epoch-{epoch:05d}_sample-{idx:04d}_decoded.png'

            # Randomly pick a sample from validation dataset to encoder
            data, *_ = self.valid_dataset[idx]
            self.sampler.plot(data, orig_path)

            # Add batch dimension and move it to device
            data_batched = data.unsqueeze(1).to(self.device)
            # Sample
            self.sampler.sample(data_batched, self.hp.temperature, decoded_path)

            if display:
                Image.open(orig_path).show()
                Image.open(decoded_path).show()
            orig_paths.append(orig_path)
            decoded_paths.append(decoded_path)
        return sorted(orig_paths), sorted(decoded_paths)   

    def step(self, batch: Any, is_training=False):
        self.encoder.train(is_training)
        self.decoder.train(is_training)

        # Move `data` and `mask` to device and swap the sequence and batch dimensions.
        # `data` will have shape `[seq_len, batch_size, 5]` and
        # `mask` will have shape `[seq_len, batch_size]`.
        data = batch[0].to(self.device).transpose(0, 1)
        mask = batch[1].to(self.device).transpose(0, 1)
        batch_items = len(data)
        
        # Get $z$, $\mu$, and $\hat{\sigma}$
        z, mu, sigma_hat = self.encoder(data)

        # Concatenate $[(\Delta x, \Delta y, p_1, p_2, p_3); z]$
        z_stack = z.unsqueeze(0).expand(data.shape[0] - 1, -1, -1)
        inputs = torch.cat([data[:-1], z_stack], 2)
        # Get mixture of distributions and $\hat{q}$
        dist, q_logits, _ = self.decoder(inputs, z, None)

        # $L_{KL}$
        kl_loss = self.kl_div_loss(sigma_hat, mu)
        if self.hp.use_eta:
            kl_loss *= self.eta_step

        # $L_R$
        reconstruction_loss = self.reconstruction_loss(mask, data[1:], dist, q_logits)
        # $Loss = L_R + w_{KL} L_{KL}$
        loss = reconstruction_loss + self.hp.kl_div_loss_weight * kl_loss

        # Only if we are in training state
        if is_training:
            # Set `grad` to zero
            self.encoder_optimizer.zero_grad()
            self.decoder_optimizer.zero_grad()
            # Compute gradients
            loss.backward()
            # Clip gradients
            nn.utils.clip_grad_norm_(self.encoder.parameters(), self.hp.grad_clip)
            nn.utils.clip_grad_norm_(self.decoder.parameters(), self.hp.grad_clip)
            # Optimize
            self.encoder_optimizer.step()
            self.decoder_optimizer.step()
        return loss.item(), reconstruction_loss.item(), kl_loss.item(), batch_items

    def validate_one_epoch(self, epoch):
        total_items, total_loss, total_kl_loss, total_reconstruction_loss = 0, 0, 0, 0
        with torch.no_grad():    
            for batch in iter(self.valid_loader):
                loss, reconstruction_loss, kl_loss, batch_items = self.step(batch, is_training=False)

                total_loss += loss * batch_items
                total_reconstruction_loss += reconstruction_loss * batch_items
                total_kl_loss += kl_loss * batch_items
                total_items += batch_items
                
        avg_loss = total_loss / total_items
        avg_reconstruction_loss = total_reconstruction_loss / total_items
        avg_kl_loss = total_kl_loss / total_items
        self.log(dict(
            val_avg_loss=avg_loss,
            val_avg_reconstruction_loss=avg_reconstruction_loss,
            val_avg_kl_loss=avg_kl_loss,
            epoch=epoch))
        return avg_loss, avg_reconstruction_loss, avg_kl_loss

    def train_one_epoch(self, epoch, parent_progressbar=None):
        steps_per_epoch = len(self.train_loader)
        for idx, batch in enumerate(progress_bar(iter(self.train_loader), parent=parent_progressbar)):
            self.total_steps = idx + epoch * steps_per_epoch
            loss, reconstruction_loss, kl_loss, _ = self.step(batch, is_training=True)
            self.log(dict(
                loss=loss,
                reconstruction_loss=reconstruction_loss,
                kl_loss=kl_loss,
                learning_rate=self.learning_rate,
                epoch=epoch))
        # update learning rate, if use_lr_decay is enabled
        if self.hp.use_lr_decay:
            self.encoder_optimizer = lr_decay(self.encoder_optimizer, self.hp.min_lr, self.hp.lr_decay)
            self.decoder_optimizer = lr_decay(self.decoder_optimizer, self.hp.min_lr, self.hp.lr_decay)
        # update weight of KL loss, if use_eta is enabled
        if self.hp.use_eta:
            self.eta_step = 1-(1-self.hp.eta_min)*self.hp.eta_R

    def train(self):
        mb = master_bar(range(self.hp.epochs))
        for epoch in mb:
            self.train_one_epoch(epoch=epoch, parent_progressbar=mb)
            val_avg_loss, *_ = self.validate_one_epoch(epoch)
            if epoch % self.hp.save_every_n_epochs == 0:
                self.save(epoch)
                self.sample(epoch)
            mb.write(f'Finished epoch {epoch}. Validation Loss: {val_avg_loss}')
