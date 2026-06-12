/// <reference types="node" />
// Loads the real app shell markup into the test DOM before the app modules are
// imported. Several modules (pipeline, errors, deepdive, segment) attach event
// listeners to index.html elements at import time and would throw on a bare
// document.
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(resolve(here, "..", "index.html"), "utf-8");
const body = html.match(/<body[^>]*>([\s\S]*)<\/body>/i);
if (!body) throw new Error("web/index.html has no <body> -- test setup cannot build the DOM");
document.body.innerHTML = body[1].replace(/<script[\s\S]*?<\/script>/gi, "");
