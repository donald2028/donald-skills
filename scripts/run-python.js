#!/usr/bin/env node
"use strict";

const { spawnSync } = require("node:child_process");

const [script, ...args] = process.argv.slice(2);
if (!script) {
  console.error("Usage: node scripts/run-python.js <script> [...args]");
  process.exit(2);
}

const preferred = process.env.PYTHON;
const candidates = preferred
  ? [[preferred, []]]
  : process.platform === "win32"
    ? [["python", []], ["py", ["-3"]]]
    : [["python3", []], ["python", []]];

for (const [command, prefix] of candidates) {
  const result = spawnSync(command, [...prefix, script, ...args], { stdio: "inherit" });
  if (result.error?.code === "ENOENT") {
    continue;
  }
  if (result.error) {
    console.error(`Could not run ${command}: ${result.error.message}`);
    process.exit(1);
  }
  process.exit(result.status ?? 1);
}

console.error("Python 3 was not found. Install Python 3.10+ or set the PYTHON environment variable.");
process.exit(1);
