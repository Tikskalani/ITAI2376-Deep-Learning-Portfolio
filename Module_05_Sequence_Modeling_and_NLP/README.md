# Module 05: Sequence Modeling and NLP

## A05: Analyzing "Arrival" Through the Lens of NLP

Assignment: use the movie Arrival (2016) to look at real natural language processing problems.

## Files

- `A05_Solo_TrilokKalani_ITAI2376.docx` - the report (APA 7th)
- `A05_Solo_TrilokKalani_ITAI2376.pptx` - slides with speaker notes

## Main idea

Louise Banks starts with zero parallel data, which is the hardest setting in machine translation and also the normal setting for most of the world's languages. The report goes through five problems from the film that are real NLP problems: no parallel corpus, deciding what counts as a token, lexical ambiguity, pragmatics, and cultural and annotation variation.

The one I liked most is "offer weapon". The ambiguous reading gets passed to eleven other sites and acted on. If the system had said weapon 0.51 / tool 0.49 instead of one word, the plot changes. That is an argument for calibrated confidence where a model's output meets people who make decisions.

## What I took away

Movies make NLP problems easy to see. A lot of what we treat as solved (tokenization, word sense) is really a modeling choice, and meaning needs more than surface statistics, which is the point Bender and Koller make.

## Main sources

- Bender, E. M., and Koller, A. (2020). Climbing towards NLU. ACL, 5185-5198
- Chiang, T. (2002). Story of your life
- Lample, G., et al. (2018). Unsupervised machine translation using monolingual corpora only. ICLR
- Joshi, P., et al. (2020). The state and fate of linguistic diversity and inclusion in the NLP world. ACL
- Levesque, H., Davis, E., and Morgenstern, L. (2012). The Winograd schema challenge
- Villeneuve, D. (Director). (2016). Arrival [Film]
