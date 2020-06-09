from pathlib import Path
from datetime import datetime

import fire

import torch
import torch.nn as nn
import torch.optim as optim

import ignite
import ignite.distributed as idist
from ignite.engine import Events, Engine, create_supervised_evaluator
from ignite.metrics import Accuracy, Loss
from ignite.handlers import Checkpoint
from ignite.utils import manual_seed

from ignite.contrib.engines import common
from ignite.contrib.handlers import ProgressBar
from ignite.contrib.handlers import PiecewiseLinear

import utils


def get_dataflow(config):
    # - Get train/test datasets
    if idist.get_rank() > 0:
        # Ensure that only rank 0 download the dataset
        idist.barrier()

    train_dataset, test_dataset = utils.get_train_test_datasets(config["data_path"])

    if idist.get_rank() == 0:
        # Ensure that only rank 0 download the dataset
        idist.barrier()

    # Setup data loader also adapted to distributed config: nccl, gloo, xla-tpu
    train_loader = idist.auto_dataloader(
        train_dataset,
        batch_size=config["batch_size"],
        num_workers=config["num_workers"],
        shuffle=True,
        pin_memory="cuda" in idist.device().type,
        drop_last=True,
    )

    test_loader = idist.auto_dataloader(
        test_dataset,
        batch_size=2 * config["batch_size"],
        num_workers=config["num_workers"],
        shuffle=False,
        pin_memory="cuda" in idist.device().type,
    )
    return train_loader, test_loader


def initialize(config):
    model = utils.get_model(config["model"])
    # Adapt model for distributed settings if configured
    model = idist.auto_model(model)

    optimizer = optim.SGD(
        model.parameters(),
        lr=config["learning_rate"],
        momentum=config["momentum"],
        weight_decay=config["weight_decay"],
        nesterov=True,
    )
    optimizer = idist.auto_optim(optimizer)
    criterion = nn.CrossEntropyLoss().to(idist.device())
    return model, optimizer, criterion


def print_metrics(epoch, tag, metrics):
    if idist.get_rank() == 0:
        print(
            "\nEpoch {} - {} metrics:\n {}".format(
                epoch, tag, "\n".join(["\t{}: {}".format(k, v) for k, v in metrics.items()])
            )
        )


def training(local_rank, config):

    rank = idist.get_rank()
    manual_seed(config["seed"] + rank)
    device = idist.device()

    if rank == 0:
        print("Train {} on CIFAR10".format(config["model"]))
        print("- PyTorch version: {}".format(torch.__version__))
        print("- Ignite version: {}".format(ignite.__version__))

        print("\n")
        print("Configuration:")
        for key, value in config.items():
            print("\t{}: {}".format(key, value))
        print("\n")

        if idist.get_world_size() > 1:
            print("\nDistributed setting:")
            print("\tbackend: {}".format(idist.backend()))
            print("\tworld size: {}".format(idist.get_world_size()))
            print("\n")

    output_path = config["output_path"]
    if rank == 0:
        if config["stop_iteration"] is None:
            now = datetime.now().strftime("%Y%m%d-%H%M%S")
        else:
            now = "stop-on-{}".format(config["stop_iteration"])

        folder_name = "{}_backend-{}-{}_{}".format(config["model"], idist.backend(), idist.get_world_size(), now)
        output_path = Path(output_path) / folder_name
        if not output_path.exists():
            output_path.mkdir(parents=True)
        config["output_path"] = output_path.as_posix()
        print("Output path: {}".format(config["output_path"]))

    # Setup dataflow, model, optimizer, criterion
    train_loader, test_loader = get_dataflow(config)

    # Setup model, optimizer
    model, optimizer, criterion = initialize(config)

    le = len(train_loader)
    milestones_values = [
        (0, 0.0),
        (le * config["num_warmup_epochs"], config["learning_rate"]),
        (le * config["num_epochs"], 0.0),
    ]
    lr_scheduler = PiecewiseLinear(optimizer, param_name="lr", milestones_values=milestones_values)

    # Setup Ignite trainer:
    # - let's define training step
    # - add other common handlers:
    #    - TerminateOnNan,
    #    - handler to setup learning rate scheduling,
    #    - ModelCheckpoint
    #    - RunningAverage` on `train_step` output
    #    - Two progress bars on epochs and optionally on iterations

    def train_step(engine, batch):

        x, y = batch[0], batch[1]

        if x.device != device:
            x = x.to(device, non_blocking=True)
            y = y.to(device, non_blocking=True)

        model.train()
        # Supervised part
        y_pred = model(x)
        loss = criterion(y_pred, y)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # This can be helpful for XLA to avoid performance slow down if fetch loss.item() every iteration
        if config["log_every_iters"] > 0 and (engine.state.iteration - 1) % config["log_every_iters"] == 0:
            batch_loss = loss.item()
            engine.state.saved_batch_loss = batch_loss
        else:
            batch_loss = engine.state.saved_batch_loss

        return {
            "batch loss": batch_loss,
        }

    trainer = Engine(train_step)
    trainer.state.saved_batch_loss = -1.0
    trainer.state_dict_user_keys.append("saved_batch_loss")

    to_save = {"trainer": trainer, "model": model, "optimizer": optimizer, "lr_scheduler": lr_scheduler}
    metric_names = [
        "batch loss",
    ]
    output_path = config["output_path"]
    common.setup_common_training_handlers(
        trainer,
        train_sampler=train_loader.sampler,
        to_save=to_save,
        save_every_iters=config["checkpoint_every"],
        output_path=output_path,
        lr_scheduler=lr_scheduler,
        output_names=metric_names if config["log_every_iters"] > 0 else None,
        with_pbar_on_iters=config["log_every_iters"] > 0,
        log_every_iters=config["log_every_iters"],
        clear_cuda_cache=False,
    )

    # Let's now setup evaluator engine to perform model's validation and compute metrics
    metrics = {
        "accuracy": Accuracy(),
        "loss": Loss(criterion),
    }

    # We define two evaluators as they wont have exactly similar roles:
    # - `evaluator` will save the best model based on validation score
    evaluator = create_supervised_evaluator(model, metrics=metrics, device=device, non_blocking=True)
    train_evaluator = create_supervised_evaluator(model, metrics=metrics, device=device, non_blocking=True)

    def run_validation(engine):
        epoch = trainer.state.epoch
        state = train_evaluator.run(train_loader)
        print_metrics(epoch, "Train", state.metrics)
        state = evaluator.run(test_loader)
        print_metrics(epoch, "Test", state.metrics)

    trainer.add_event_handler(Events.EPOCH_COMPLETED(every=config["validate_every"]) | Events.COMPLETED, run_validation)

    if rank == 0:
        # Setup progress bar on evaluation engines
        if config["log_every_iters"] > 0:
            ProgressBar(persist=False, desc="Train evaluation").attach(train_evaluator)
            ProgressBar(persist=False, desc="Test evaluation").attach(evaluator)

        # Setup TensorBoard logging on trainer and evaluators. Logged values are:
        #  - Training metrics, e.g. running average loss values
        #  - Learning rate
        #  - Evaluation train/test metrics
        evaluators = {"training": train_evaluator, "test": evaluator}
        tb_logger = common.setup_tb_logging(output_path, trainer, optimizer, evaluators=evaluators)

        trains_logger = common.setup_trains_logging(
            trainer, optimizer, evaluators=evaluators, project_name="cifar10-ignite", task_name=Path(output_path).stem
        )

    # Store 3 best models by validation accuracy:
    common.save_best_model_by_val_score(
        output_path, evaluator, model=model, metric_name="accuracy", n_saved=3, trainer=trainer, tag="test"
    )

    # In order to check training resuming we can stop training on a given iteration
    if config["stop_iteration"] is not None:

        @trainer.on(Events.ITERATION_STARTED(once=config["stop_iteration"]))
        def _():
            print("Stop training on {} iteration".format(trainer.state.iteration))
            trainer.terminate()

    resume_from = config["resume_from"]
    if resume_from is not None:
        checkpoint_fp = Path(resume_from)
        assert checkpoint_fp.exists(), "Checkpoint '{}' is not found".format(checkpoint_fp.as_posix())
        if rank == 0:
            print("Resume from a checkpoint: {}".format(checkpoint_fp.as_posix()))
        checkpoint = torch.load(checkpoint_fp.as_posix(), map_location="cpu")
        Checkpoint.load_objects(to_load=to_save, checkpoint=checkpoint)

    try:
        trainer.run(train_loader, max_epochs=config["num_epochs"])
    except Exception as e:
        import traceback

        print(traceback.format_exc())

    if rank == 0:
        tb_logger.close()
        trains_logger.close()


def run(
    seed=543,
    data_path="/tmp/cifar10",
    output_path="/tmp/output-cifar10/",
    model="resnet18",
    batch_size=512,
    momentum=0.9,
    weight_decay=1e-4,
    num_workers=12,
    num_epochs=24,
    learning_rate=0.4,
    num_warmup_epochs=4,
    validate_every=3,
    checkpoint_every=200,
    backend=None,
    resume_from=None,
    log_every_iters=15,
    num_procs_per_node=None,
    stop_iteration=None,
    **spawn_kwargs
):
    """Main entry to train an model on CIFAR10 dataset.

    Args:
        seed (int): random state seed to set. Default, 543.
        data_path (str): input dataset path. Default, "/tmp/cifar10".
        output_path (str): output path. Default, "/tmp/output-cifar10".
        model (str): model name (from torchvision) to setup model to train. Default, "resnet18".
        batch_size (int): total batch size. Default, 512.
        momentum (float): optimizer's momentum. Default, 0.9.
        weight_decay (float): weight decay. Default, 1e-4.
        num_workers (int): number of workers in the data loader. Default, 5.
        num_epochs (int): number of epochs to train the model. Default, 24.
        learning_rate (float): peak of piecewise linear learning rate scheduler. Default, 0.4.
        num_warmup_epochs (int): number of warm-up epochs before learning rate decay. Default, 4.
        validate_every (int): run model's validatation every ``validate_every`` epochs. Default, 3.
        checkpoint_every (int): store training checkpoint every ``checkpoint_every`` iterations. Default, 200.
        backend (str, optional): backend to use for distributed configuration. Possible values: None, "nccl", "xla-tpu",
            "gloo" etc. Default, None.
        num_procs_per_node (int, optional): optional argument to setup number of processes per node. It is useful,
            when main python process is spawning training as child processes.
        resume_from (str, optional): path to checkpoint to use to resume the training from. Default, None.
        log_every_iters (int): argument to log progress every ``log_every_iters`` iterations. It can be 0 to disable it.
            Default, 15.
        stop_iteration (int, optional): iteration to stop the training. Can be used to check resume from checkpoint.
        **spawn_kwargs: Other kwargs to spawn training as child processes.

    """
    # catch all local parameters
    config = locals()
    config.update(config["spawn_kwargs"])
    del config["spawn_kwargs"]

    spawn_kwargs["num_procs_per_node"] = num_procs_per_node

    with idist.Parallel(backend=backend, **spawn_kwargs) as parallel:
        parallel.run(training, config)


if __name__ == "__main__":
    fire.Fire({"run": run})
