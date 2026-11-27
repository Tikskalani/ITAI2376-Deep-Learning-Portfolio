# Module 02: Deep Learning Environments, Libraries and Tools

## A02: PyTorch Lightning vs fastai

Assignment: a comparative analysis of two machine learning / deep learning tools. My pair was PyTorch Lightning and fastai, two libraries that both sit on top of PyTorch.

## Files

- `A02a_Solo_TrilokKalani_ITAI2376.pptx` - presentation with speaker notes (background, key features, real-world evidence, side by side comparison, demo)
- `A02b_Solo_TrilokKalani_ITAI2376.docx` - one page written summary with sources
- `A02_demo_lightning_vs_fastai.ipynb` - demo notebook, executed with outputs saved: the same small CNN on MNIST trained once with a Lightning Trainer and once with a fastai Learner, plus fastai's pretrained ResNet34 quick start on the Pets dataset

Results from my CPU-only run: both libraries trained the same CNN in about 34 seconds for 2 epochs (Lightning 98.45% validation accuracy, fastai 98.15%), and the pretrained ResNet34 reached a 0.47% error rate on cats vs dogs after one fine-tuning epoch. The small accuracy gap comes from the different learning-rate schedules, not from the libraries.

## What I took away

Plain PyTorch makes you write the training loop yourself, and both libraries fix that, but in opposite directions. Lightning keeps your own model code and takes the engineering off your hands (the loop, checkpointing, mixed precision, multi-GPU strategies like FSDP). fastai gives you strong defaults for data, transfer learning and the training schedule, so you get a good result fast and open up the layers underneath later. Lightning started as a research tool and fastai started as a teaching tool, and that explains most of the differences.

I also learned to be careful with claims. I could not find an independent benchmark of the two, so I did not call a performance winner, and I marked the numbers that were self-reported (like fast.ai's DAWNBench result).

## Main sources

- Howard, J., and Gugger, S. (2020). fastai: A layered API for deep learning. arXiv:2002.04688
- Howard, J., and Ruder, S. (2018). Universal language model fine-tuning for text classification (ULMFiT). arXiv:1801.06146
- PyTorch Lightning documentation, https://lightning.ai/docs/pytorch/stable/
- fastai documentation, https://docs.fast.ai/
