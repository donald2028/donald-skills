# Transfer Inputs And Results

Choose the method that preserves the user's material and is supported by the current UI. Describe
files by their role in the prompt, especially when comparing several attachments. A local path in
message text does not upload a file; a pasted URL does not prove ChatGPT fetched its contents.

| Material | Preferred method | Evidence before continuing |
| --- | --- | --- |
| Short text, code, links | Fill the composer; use `text_file` for literal multiline input | Read back the composer, including the end of long text |
| Long text | Attach the original text/Markdown file when suitable, with a concise instruction | Ready attachment plus the intended instruction |
| Documents, spreadsheets, PDFs, images, screenshots, data exports | The visible Add files/Upload control and its actual file input | Each expected attachment card/name is present, with no progress, failure, or pending processing |
| Multiple files | Upload the intended ordered list, then wait for every attachment | Names and roles match; inspect ambiguous/truncated names before Send |
| An existing library/cloud file | The account's visible picker, when requested and already connected | The specific selected file is attached; a visible menu entry alone is insufficient |
| Text supplied from a clipboard | Pass the supplied text or a local text file to `fill` | Draft content matches; the user's system clipboard is untouched |
| Drag/drop or pasted image | Prefer the file input for the same local bytes | The normal upload checks still apply; use actual browser support rather than invented clipboard/file APIs |
| Returned text, Markdown, tables, code | Capture rendered messages; request a Markdown file or fenced text when literal Markdown matters | Correct user turn, stable finished reply, complete code and citations |
| Returned files | Download the actual response link/control through the authenticated browser | The file exists locally, has nonzero content, and is the expected file type rather than an HTML error page |

Use absolute paths for uploads, `text_file`, saved responses, and downloads. Validate local sources
before interacting. For a hidden file input, open the upload menu and inspect the DOM to identify
the real `input[type=file]`; do not assume the first input is the right one. After selection, wait
for ready composer evidence. If files share a name, use a clear model-facing role map and only
create distinct upload aliases when necessary; preserve the originals.

The session bridge accepts direct browser commands such as:

```json
{"op":"browser","args":["fill","@e3"],"text_file":"/absolute/path/prompt.md"}
{"op":"browser","args":["upload","input[type=file]","/absolute/path/a.pdf","/absolute/path/b.csv"]}
{"op":"browser","args":["download","@e9","/absolute/path/output/report.docx"]}
{"op":"eval","script":"document.querySelector('input[type=file]')?.accept"}
```

Refs and selectors must come from current observation. `eval` can inspect file types and the
rendered draft, but must not read hidden credentials or undocumented ChatGPT backend APIs. Use the
helper's `output_dir` for downloaded files unless the user supplied another destination. Open file
previews only when needed to obtain the real download control. Keep `sandbox:` links as evidence;
they are not verified local files. If a download fails, retry collection from the existing response
once; never regenerate the answer just to obtain a file.

For material reachable only through another connected service, use that service's authorized
download/export capability first, then attach the resulting local file when needed. Do not silently
connect an account, widen permissions, upload unrelated material, or assume every format, archive,
audio/video input, file size, or cloud import is supported. Report the visible limitation and use a
faithful alternative only when it preserves the task (for example, an extracted text attachment
when layout is irrelevant). Otherwise ask for the material decision that is missing.
