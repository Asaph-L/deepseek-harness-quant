#!/usr/bin/env node
"use strict";

// Compile every inline UI script without executing browser side effects.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const base = path.resolve(__dirname, "..");
const pages = path.join(base, "ui_v2", "pages");
let blocks = 0;

for (const name of fs.readdirSync(pages).filter((value) => value.endsWith(".html")).sort()) {
  const source = fs.readFileSync(path.join(pages, name), "utf8");
  const pattern = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi;
  for (const match of source.matchAll(pattern)) {
    if (!match[1].trim()) continue;
    new vm.Script(match[1], { filename: `ui_v2/pages/${name}` });
    blocks += 1;
  }
}

if (!blocks) throw new Error("NO_INLINE_UI_SCRIPTS_FOUND");
process.stdout.write(`inline-ui-js: ${blocks} blocks ok\n`);
