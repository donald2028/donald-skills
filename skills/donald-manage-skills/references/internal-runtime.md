# Internal Runtime

Run the checks relevant to the change from the repository root:

```bash
npm run build
npm run build:check
npx skills add . --list
claude plugin validate . --strict
gemini extensions validate .
```

When the local environment provides a runtime-specific skill or plugin validator, run it against
the changed skill or repository as well. Validate JSON manifests with the standard library when a
runtime CLI is unavailable.
