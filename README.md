# ITAI 2376 Deep Learning Portfolio

Trilok Kalani | Houston Community College | Fall 2026 | Section 6271

This repo holds all of my work for ITAI 2376, organized by the course modules on Canvas. Modules with a graded assignment have the files I turned in plus a README that says what was asked, what I turned in, what I took away, and the sources. Modules without their own assignment have a short README that points to where that module's ideas show up in my work. The final presentation that walks through the portfolio is in `Final_Portfolio`.

## Folders

| Folder | Graded work | What is inside |
|---|---|---|
| [Module_01_Course_Introduction](Module_01_Course_Introduction) | none | Pointer only |
| [Module_02_DL_Tools_and_Frameworks](Module_02_DL_Tools_and_Frameworks) | A02 Comparative analysis of DL tools (PyTorch Lightning vs fastai) | Slides, written summary, demo notebook (executed) |
| [Module_03_Neural_Network_Basics](Module_03_Neural_Network_Basics) | A03 Neural Network Zoo (the Transformer, drawn as an octopus) | Slides (PPTX and PDF), script, field guide poster |
| [Module_04_CNNs](Module_04_CNNs) | A04 Deep learning for an 11 year old (gradient descent) | Slides with speaker notes, figures |
| [Module_05_Sequence_Modeling_and_NLP](Module_05_Sequence_Modeling_and_NLP) | A05 Analyzing "Arrival" through the lens of NLP | Report, slides |
| [Module_06_Transformers](Module_06_Transformers) | none | Pointer to A03 |
| [Module_07_VAEs](Module_07_VAEs) | none | Pointer to A03 and the midterm |
| [Module_08_GANs](Module_08_GANs) | none | Pointer to A03 and the midterm |
| [Module_09_Diffusion_Models_Midterm](Module_09_Diffusion_Models_Midterm) | Midterm: creating images with diffusion models | Notebook (executed), report, figures |
| [Module_10_RL_and_AI_Agents](Module_10_RL_and_AI_Agents) | none | Where the RL ideas are in my capstone |
| [Module_11_Reasoning_and_RAG](Module_11_Reasoning_and_RAG) | none | Where retrieval and ReAct are in my capstone |
| [Module_12_Agent_Planning_and_Orchestration](Module_12_Agent_Planning_and_Orchestration) | none | Where planning and dispatch are in my capstone |
| [Module_13_AI_Agents](Module_13_AI_Agents) | none | Where the tool loop and gates are in my capstone |
| [Module_14_Multi_Agent_Systems_Capstone](Module_14_Multi_Agent_Systems_Capstone) | Capstone: AI Agent Creation (Option 3, multi-agent system) | Proposal, report, demo video, code |
| [Final_Portfolio](Final_Portfolio) | Final exam portfolio | Presentation (PPTX and PDF) |

I worked solo all semester, so the "group" files for the capstone list me as the only member.

## How the course built up

The assignments ended up stacking on each other more than I expected. A02 was about the tools (what a training loop is and who should write it). A04 was the math underneath every training loop, gradient descent, explained simply enough for a kid. A03 was about architectures, and I went deep on the Transformer and self-attention. The midterm was the first time I trained a generative model from scratch and actually had to measure whether it worked. A05 used a movie to look at the parts of language that models still struggle with. The capstone put language models to work as agents with tools, memory and rules around them.

## Key articles and videos

These are the sources that did the most for me this semester. Each one is cited inside the assignment where I used it.

- Vaswani et al. (2017), *Attention Is All You Need*. https://arxiv.org/abs/1706.03762 (A03)
- Ho, Jain and Abbeel (2020), *Denoising Diffusion Probabilistic Models*. https://arxiv.org/abs/2006.11239 (Midterm)
- Howard and Gugger (2020), *fastai: A Layered API for Deep Learning*. https://arxiv.org/abs/2002.04688 (A02)
- Bender and Koller (2020), *Climbing towards NLU: On Meaning, Form, and Understanding in the Age of Data*. https://aclanthology.org/2020.acl-main.463/ (A05)
- Alammar (2018), *The Illustrated Transformer*. https://jalammar.github.io/illustrated-transformer/ (A03)
- 3Blue1Brown, *Gradient descent, how neural networks learn* (video). https://www.youtube.com/watch?v=IHZwWFHWa-w (A04)
- 3Blue1Brown, *Attention in transformers, step-by-step* (video). https://www.youtube.com/watch?v=eMlx5fFNoYc (A03)

## Running the notebooks

The notebooks are plain Jupyter and run on Python 3.12 with PyTorch. The midterm notebook was run on CPU only, so the config cell uses a smaller setting than the default (it says so in the cell). On a Colab GPU the full settings run without changes. The capstone code has its own setup steps in `Module_14_Multi_Agent_Systems_Capstone/code/atlas/README.md`.

## Use of AI tools

I used Claude (Anthropic) as a study and drafting helper on several assignments, mostly to check my explanations, debug code and tighten writing. The results and figures come from my own runs, and I checked every source I cite. The midterm report has its own AI-use statement.

## Attribution

Everything here is my own work unless a source is cited in the file. Papers, docs and videos are credited where they are used, and the full reference lists are in each report.
