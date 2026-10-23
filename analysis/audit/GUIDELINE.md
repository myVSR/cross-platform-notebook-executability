# Annotation guideline: manual audits of the MSR 2027 paper

Two short audits check two measurements of the pipeline. Each annotator works **alone**: please don't discuss items
until both of you have downloaded your answers. Disagreements are settled afterwards, together (see "After
labelling").

- **Time:** about 2–3 hours in total.
- **Blind:** the pages never show the pipeline's own label.
- **Confidential:** the items come from third-party notebooks. Keep the files to yourself and delete them afterwards.

**How to work**
1. Open `normalisation.html` (Audit A) and `failures.html` (Audit B) in a browser (Firefox or Chrome).
2. Enter your initials at the top.
3. Answer the items **in the order shown, from the top**. The order is random. If time runs short, stop wherever you
   are and send what you have; please try for at least the first 50 items of each part. A comment is optional but
   welcome for borderline cases.
4. Your answers are kept in that browser. You can close the page and continue later, in the same browser on the same
   computer.
5. When done, press **Download answers (CSV)** on each page and send both CSV files to the authors.

## Part A: stored vs re-executed outputs (120 items)

Each item shows one code cell:
- its source;
- the output the authors **stored** in the published notebook;
- the output of our **re-execution**.

Question: **would a reader get the same result from both outputs?**

**Same result:** the outputs differ only in incidental content. Typical cases:
- memory addresses (`<object at 0x7f…>`), timestamps of the run, elapsed times, temporary or absolute paths, user names;
- progress bars, download messages, deprecation or other warnings printed next to the result;
- widget or plot identifiers, the order of keys in a printed dict or set when the order carries no meaning;
- images that show the same plot, perhaps with a different font, size, encoding or anti-aliasing;
- floating-point noise in the last digits that would not change any conclusion (e.g. `0.30000000000000004` vs `0.3`).

**Different result:** a reader would read off something different. Typical cases:
- a different number, count, table content, model score, or a plot with different data, shape or labels;
- a random sample, shuffled split or training run that gives other values, even if the code is "correct";
- an error message or a warning *instead of* the result, or a result *missing*;
- text output that states something else (e.g. another file name or version that the analysis depends on).

**Cannot judge:** use this only if the item really does not show enough. For example, an image fails to load, or the
outputs are cut so much that the decisive part is missing. An empty box where an output should be is usually an
interactive output (an animation, a widget) that the page cannot run: choose cannot judge, not same.

Rules of thumb:
- **Look at what the cell is for.** If a timestamp *is* the result (e.g. the cell prints when a measurement was
  taken), a different timestamp is a different result.
- **Random numbers:** if a cell draws random numbers (a sample, a shuffled split, random weights, a training run)
  and the values differ, the result is **different**, whether or not a seed is set. A seed that does not give the
  same numbers is exactly what the study measures. Randomness that the analysis does not use (a random ID, a
  temporary file name, a UUID) is incidental: **same**. Write `random` in the comment for these cells.
- **Long decimals that differ only at the end** (e.g. `80.22760181924441` vs `80.22760199687973`): round both to the
  precision the authors would report, about 4 significant digits. If they then match, the result is **same**. It
  is **different** only if the cell depends on those digits: it tests exact equality, or a rounding, threshold or
  ranking changes, or later cells drift visibly. Write `float` in the comment for these cells.
- **When in doubt between same and different, choose different** and add a comment.

## Part B: causes of failure (100 items)

Each item shows the **first error** of a notebook that did not run cleanly in our re-execution:
- the failing cell and its error output;
- up to three earlier cells;
- any earlier cell that asks for input or credentials;
- the packages that could not be installed for the resource.

Question: **what is the cause of this failure?** Choose the cause, not the exception type.

| Label | Choose it when |
|---|---|
| **Dependency** | A module is missing, a package could not be installed, or the installed version does not fit. This includes an `AttributeError`, `TypeError` or `ImportError` raised inside a library because a newer version removed or renamed something, and a library that needs a missing system component *to import*. |
| **Missing file** | A file or folder the notebook reads is not there: not part of the resource, an absolute path from the author's machine (`C:\Users\…`, `/content/drive/…`), or a file a previous, unshipped step would have produced. |
| **Data** | The data is present but its content or format does not fit what the code expects (wrong columns, parse errors, empty data). |
| **Code** | A fault in the notebook's own code: cells meant to run in another order, an undefined name, a typo, a syntax error, wrong use of an API that never worked this way. Code in another language run as Python (e.g. R syntax) counts as code. |
| **External service** | The notebook needs credentials, an API key, a login or an interactive input that nobody gives in an unattended run, or a remote service answered with an error (HTTP 401/403/429/5xx, quota, rate limit). A missing secret environment variable counts here. |
| **Network** | A remote host could not be reached at all (DNS failure, connection refused or timed out) and nothing points to credentials. |
| **Environment** | The machine does not provide what is needed: GPU/CUDA, memory, a display, an operating-system feature, a system program, or the kernel died. |
| **No execution** | The notebook could not be run at all. The item then shows a pipeline message instead of a cell: an invalid file, a crashed kernel, or the 30-minute time limit. |
| **Other** | A clear cause that fits none of the above (say which in the comment). |
| **Cannot judge** | The item does not show enough to decide. |

Rules of thumb:
- **Use the first decisive cause.** If a credential prompt in an earlier cell left a variable empty and the failure is a
  later `AttributeError` on that variable, the cause is **External service**.
- **Missing module or package:** if the failing import names a module of one of the "packages that could not be
  installed", the cause is **Dependency**, whatever the exception type.
- **A file that a package should download at first use** (models, datasets): if the download needs a login, choose
  **External service**; if the host is unreachable, choose **Network**; if the URL is gone (404), choose **Missing
  file**.

## After labelling

the authors merges the two CSV files with `analysis/audit/score_audits.py`. It reports:
- agreement between the two annotators (Cohen's κ);
- for the items where both agree, how often the pipeline's label matches.

Items on which the annotators disagree are then discussed and settled together. The settled labels go into a third
CSV (`adjudicated.csv`, same columns, annotator `ADJ`), and the script is run again.
