import { readFile } from "node:fs/promises";
import ts from "typescript";

const urls = new Map();
async function sourceUrl(name) {
  if (urls.has(name)) return urls.get(name);
  const isJson = name.endsWith('.json');
  const raw = await readFile(new URL(`../src/${name}${isJson ? '' : '.ts'}`, import.meta.url), "utf8");
  const source = isJson ? `export default ${JSON.stringify(JSON.parse(raw))};` : raw;
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ES2020, target: ts.ScriptTarget.ES2020 },
  });
  for (const match of [...outputText.matchAll(/from ["']\.\/([^"']+)["']/g)]) {
    outputText = outputText.replace(match[0], `from "${await sourceUrl(match[1])}"`);
  }
  // A data URL has no node_modules location. Resolve package imports here so
  // source-level tests exercise the same installed dependency as the app.
  for (const match of [...outputText.matchAll(/from ["']([^./:][^:"']*)["']/g)]) {
    outputText = outputText.replace(match[0], `from "${import.meta.resolve(match[1])}"`);
  }
  const url = `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
  urls.set(name, url);
  return url;
}
export async function sourceModule(name) { return import(await sourceUrl(name)); }
