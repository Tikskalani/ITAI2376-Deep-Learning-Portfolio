# Module 03: Neural Network Basics

## A03: Neural Network Zoo, the Transformer Octopus

Assignment: pick an animal for a neural network architecture and explain the architecture through it. I picked the Transformer and made it an octopus.

## Files

- `A03_Solo_TrilokKalani_ITAI2376.pptx` / `.pdf` - the slides
- `A03_Solo_TrilokKalani_ITAI2376_Script.docx` - my presenter script
- `A03_Solo_TrilokKalani_ITAI2376_Poster.pdf` / `.png` - a one page "field guide" poster for the octopus

## Why an octopus

The original Transformer has 8 attention heads, so 8 arms, each reaching for a different relationship between words. All the arms move at once, the same way a Transformer looks at every word in the sentence at the same time instead of one word at a time like an RNN. And about two thirds of an octopus's neurons are in its arms (Hochner, 2012), which fit the idea that each head learns its own pattern without being told what to look for.

## What I took away

The part that finally clicked for me was the formula softmax(QK^T / sqrt(d_k)) V. Each word sends a query, every word answers with a key, and the best matches share their values. In the poster, when the model reads "it", its strongest grip is on "animal". I also wrote down the weak spots: cost grows with the square of the sequence length, it has no sense of word order without positional encoding, and it can sound sure while being wrong.

## Main sources

- Vaswani, A., et al. (2017). Attention is all you need. NeurIPS. arXiv:1706.03762
- Devlin, J., et al. (2019). BERT. NAACL
- Dosovitskiy, A., et al. (2021). An image is worth 16x16 words (ViT). ICLR
- Alammar, J. (2018). The Illustrated Transformer. https://jalammar.github.io/illustrated-transformer/
- Hochner, B. (2012). An embodied view of octopus neurobiology. Current Biology
- Video: 3Blue1Brown, Attention in transformers, step-by-step. https://www.youtube.com/watch?v=eMlx5fFNoYc
