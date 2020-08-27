import os
from functools import partial
import numpy as np
import random
import argparse
import torch
import torch.nn.functional as F
import ignite
import logging
import workflow
from workflow import json
from workflow.functional import starcompose
from workflow.torch import set_seeds, module_eval
from workflow.ignite import worker_init
from workflow.ignite.handlers.learning_rate import (
    LearningRateScheduler, warmup, cyclical
)
from datastream import Datastream
from simple_pid import PID

from vae import datastream, architecture, metrics



def train(config):

    set_seeds(config['seed'])

    device = torch.device('cuda' if config['use_cuda'] else 'cpu')

    model = architecture.Model(config).to(device)
    # prior = model.prior()
    optimizer = torch.optim.Adamax(
        model.parameters(), lr=config['learning_rate']
    )
    # model.eval()
    # prior.train()

    train_state = dict(model=model, optimizer=optimizer)

    if os.path.exists('model'):
        print('Loading model checkpoint')
        workflow.ignite.handlers.ModelCheckpoint.load(
            train_state, 'model/checkpoints', device
        )

        workflow.torch.set_learning_rate(optimizer, config['learning_rate'])

    n_parameters = sum([
        p.shape.numel() for p in model.parameters() if p.requires_grad
    ])
    print(f'n_parameters: {n_parameters:,}')

    def process_batch(examples):
        predictions = model.prediction(
            architecture.FeaturesBatch.from_examples(examples)
        )
        return predictions, predictions.loss(examples)

    
    @workflow.ignite.decorators.train(model, optimizer)
    def train_batch(engine, examples):
        # with workflow.torch.requires_nograd(model):
        #     with workflow.torch.requires_grad(prior):
        #         predictions, loss = process_batch(examples)
        #         loss.backward()

        predictions, loss = process_batch(examples)
        loss.backward()

        return dict(
            examples=examples,
            predictions=predictions.cpu().detach(),
            loss=loss,
        )


    @workflow.ignite.decorators.evaluate(model)
    def evaluate_batch(engine, examples):
        predictions, loss = process_batch(examples)
        return dict(
            examples=examples,
            predictions=predictions.cpu().detach(),
            loss=loss,
        )

    evaluate_data_loaders = {
        f'evaluate_{name}': datastream.data_loader(
            batch_size=config['eval_batch_size'],
            num_workers=config['n_workers'],
            collate_fn=tuple,
        )
        for name, datastream in datastream.evaluate_datastreams().items()
        if 'compare' in name
    }

    trainer, evaluators, tensorboard_logger = workflow.ignite.trainer(
        train_batch,
        evaluate_batch,
        evaluate_data_loaders,
        metrics=dict(
            progress=metrics.progress_metrics(),
            train=metrics.train_metrics(),
            **{
                name: metrics.evaluate_metrics()
                for name in evaluate_data_loaders.keys()
            },
        ),
        optimizers=optimizer,
    )

    workflow.ignite.handlers.ModelScore(
        # lambda: -evaluators['evaluate_early_stopping'].state.metrics['mse'],
        lambda: trainer.state.epoch,
        train_state,
        {
            name: metrics.evaluate_metrics()
            for name in evaluate_data_loaders.keys()
        },
        tensorboard_logger,
        config,
    ).attach(trainer, evaluators)


    def log_examples(description):
        @torch.no_grad()
        def log_examples_(engine, logger, event_name):
            n_examples = 5
            indices = np.random.choice(
                len(engine.state.output['predictions']),
                n_examples,
                replace=False,
            )

            logger.writer.add_images(
                f'{description}/predictions',
                np.stack([
                    np.concatenate([
                        np.array(
                            engine.state.output['examples'][index]
                            .representation()
                        ),
                        np.array(
                            engine.state.output['predictions'][index]
                            .representation()
                        ),
                    ], axis=0) / 255
                    for index in indices
                ]),
                trainer.state.epoch,
                dataformats='NHWC',
            )

            with torch.no_grad(), module_eval(model) as eval_model:
                samples = eval_model.generated(5)

            logger.writer.add_images(
                f'{description}/samples',
                np.stack([
                    np.array(sample.representation())
                    for sample in samples
                ]) / 255,
                trainer.state.epoch,
                dataformats='NHWC',
            )
        return log_examples_
    
    tensorboard_logger.attach(
        trainer,
        log_examples('train'),
        ignite.engine.Events.EPOCH_COMPLETED,
    )

    for name, evaluator in evaluators.items():
        tensorboard_logger.attach(
            evaluator, log_examples(name), ignite.engine.Events.EPOCH_COMPLETED
        )

    trainer.run(
        data=(
            datastream.GradientDatastream()
            .data_loader(
                batch_size=config['batch_size'],
                num_workers=config['n_workers'],
                n_batches_per_epoch=config['n_batches_per_epoch'],
                worker_init_fn=partial(worker_init, config['seed'], trainer),
                collate_fn=tuple,
            )
        ),
        max_epochs=config['max_epochs'],
    )
