#!/usr/bin/env node

import { createHash } from "node:crypto";
import { createReadStream, createWriteStream } from "node:fs";
import {
  chmod,
  copyFile,
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rename,
  rm,
  stat,
} from "node:fs/promises";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { spawn } from "node:child_process";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const configPath = join(repoRoot, "external-assets.json");
const packagePath = join(repoRoot, "package.json");
const skillsBin = join(repoRoot, "node_modules", ".bin", "skills");
const cliTarget = join(repoRoot, "cli");
const skillsTarget = join(repoRoot, "skills");

const mode = process.argv[2] ?? "--all";
if (!["--all", "--skills-only", "--cli-only"].includes(mode) || process.argv.length > 3) {
  throw new Error("用法：npm run assets:install，或 npm run assets:skills / assets:cli");
}
const installSkills = mode !== "--cli-only";
const installCli = mode !== "--skills-only";

function validateConfig(config) {
  const safeName = /^[a-z0-9][a-z0-9._-]*$/;
  const commit = /^[0-9a-f]{40}$/;
  const sha256 = /^[0-9a-f]{64}$/;
  const requireHttps = (value, label) => {
    try {
      if (new URL(value).protocol !== "https:") throw new Error();
    } catch {
      throw new Error(`${label} 必须是 HTTPS URL`);
    }
  };
  const requireName = (value, label) => {
    if (typeof value !== "string" || !safeName.test(value)) {
      throw new Error(`${label} 不是安全名称`);
    }
  };
  const requireRelative = (value, label) => {
    const parts = typeof value === "string" ? value.replaceAll("\\", "/").split("/") : [];
    if (!value || isAbsolute(value) || parts.includes("..") || parts.includes("")) {
      throw new Error(`${label} 必须是安全的相对路径`);
    }
  };

  if (
    config?.schemaVersion !== 1 ||
    config.platform !== "linux-amd64" ||
    !Array.isArray(config.skills) ||
    !Array.isArray(config.cli)
  ) {
    throw new Error("external-assets.json 的 schemaVersion、platform、skills 或 cli 无效");
  }

  const ids = new Set();
  const skillNames = new Set();
  for (const [index, source] of config.skills.entries()) {
    const label = `skills[${index}]`;
    requireName(source.id, `${label}.id`);
    requireName(source.group, `${label}.group`);
    requireHttps(source.repository, `${label}.repository`);
    requireRelative(source.licensePath, `${label}.licensePath`);
    if (!commit.test(source.revision) || !Array.isArray(source.skills) || source.skills.length === 0) {
      throw new Error(`${label} 必须声明完整 commit SHA 和至少一个 Skill`);
    }
    if (ids.has(source.id)) throw new Error(`重复的 Skill 来源 id：${source.id}`);
    ids.add(source.id);
    for (const name of source.skills) {
      requireName(name, `${label}.skills`);
      if (skillNames.has(name)) throw new Error(`重复的 Skill 名称：${name}`);
      skillNames.add(name);
    }
  }

  const tools = new Set();
  const commands = new Set();
  for (const [index, tool] of config.cli.entries()) {
    const label = `cli[${index}]`;
    requireName(tool.name, `${label}.name`);
    requireName(tool.binaryName, `${label}.binaryName`);
    requireName(tool.installAs, `${label}.installAs`);
    requireHttps(tool.url, `${label}.url`);
    requireHttps(tool.licenseUrl, `${label}.licenseUrl`);
    if (
      typeof tool.version !== "string" ||
      !["tar.gz", "zip", "binary"].includes(tool.format) ||
      !sha256.test(tool.archiveSha256) ||
      !sha256.test(tool.binarySha256)
    ) {
      throw new Error(`${label} 的 version、format 或 SHA-256 无效`);
    }
    if (tools.has(tool.name) || commands.has(tool.installAs)) {
      throw new Error(`重复的 CLI 名称或安装名：${tool.name}/${tool.installAs}`);
    }
    tools.add(tool.name);
    commands.add(tool.installAs);
  }
}

function run(command, args, { cwd = repoRoot, capture = false, env = process.env } = {}) {
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(command, args, {
      cwd,
      env,
      stdio: capture ? ["ignore", "pipe", "pipe"] : "inherit",
    });
    let stdout = "";
    let stderr = "";
    if (capture) {
      child.stdout.setEncoding("utf8");
      child.stderr.setEncoding("utf8");
      child.stdout.on("data", (chunk) => (stdout += chunk));
      child.stderr.on("data", (chunk) => (stderr += chunk));
    }
    child.on("error", (error) =>
      rejectPromise(new Error(`无法执行 ${command}：${error.message}`)),
    );
    child.on("close", (code, signal) => {
      if (code === 0) return resolvePromise({ stdout, stderr });
      const reason = signal ? `signal ${signal}` : `exit ${code}`;
      rejectPromise(
        new Error(
          `${command} ${args.join(" ")} 执行失败（${reason}）${stderr.trim() ? `\n${stderr.trim()}` : ""}`,
        ),
      );
    });
  });
}

async function exists(path) {
  try {
    await lstat(path);
    return true;
  } catch (error) {
    if (error.code === "ENOENT") return false;
    throw error;
  }
}

async function assertReplaceable(path) {
  if (!(await exists(path))) return;
  const info = await lstat(path);
  if (!info.isDirectory() || info.isSymbolicLink()) {
    throw new Error(`拒绝替换非普通目录：${path}`);
  }
}

async function fileSha256(path) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest("hex");
}

async function download(url, destination) {
  console.log(`下载 ${url}`);
  const response = await fetch(url, {
    redirect: "follow",
    headers: { "user-agent": "hermes-agents-by-kanban-assets/1" },
  });
  if (!response.ok || !response.body) {
    throw new Error(`下载失败：${url}（HTTP ${response.status}）`);
  }
  await pipeline(Readable.fromWeb(response.body), createWriteStream(destination, { mode: 0o600 }));
}

function assertSafeArchive(entries, name) {
  for (const raw of entries.split(/\r?\n/)) {
    if (!raw) continue;
    const normalized = raw.replaceAll("\\", "/");
    if (normalized.startsWith("/") || normalized.split("/").includes("..")) {
      throw new Error(`${name} archive 包含不安全路径：${raw}`);
    }
  }
}

async function extract(tool, archive, destination) {
  await mkdir(destination, { recursive: true });
  if (tool.format === "binary") {
    await copyFile(archive, join(destination, tool.binaryName));
    return;
  }
  if (tool.format === "tar.gz") {
    const listing = await run("tar", ["-tzf", archive], { capture: true });
    assertSafeArchive(listing.stdout, tool.name);
    await run("tar", ["-xzf", archive, "-C", destination]);
    return;
  }
  const listing = await run("unzip", ["-Z1", archive], { capture: true });
  assertSafeArchive(listing.stdout, tool.name);
  await run("unzip", ["-q", archive, "-d", destination]);
}

async function findNamedFiles(root, expectedName) {
  const matches = [];
  async function visit(current) {
    for (const entry of await readdir(current, { withFileTypes: true })) {
      const path = join(current, entry.name);
      if (entry.isDirectory()) await visit(path);
      else if (entry.isFile() && entry.name === expectedName) matches.push(path);
    }
  }
  await visit(root);
  return matches;
}

async function cloneSource(source, destination) {
  await mkdir(destination, { recursive: true });
  await run("git", ["init", "--quiet", destination]);
  await run("git", ["-C", destination, "remote", "add", "origin", source.repository]);
  await run("git", [
    "-C",
    destination,
    "fetch",
    "--quiet",
    "--depth",
    "1",
    "origin",
    source.revision,
  ]);
  const actual = (await run("git", ["-C", destination, "rev-parse", "FETCH_HEAD"], {
    capture: true,
  })).stdout.trim();
  if (actual !== source.revision) {
    throw new Error(`${source.id} revision 不匹配：期望 ${source.revision}，实际 ${actual}`);
  }
  await run("git", ["-C", destination, "checkout", "--quiet", "--detach", "FETCH_HEAD"]);
}

async function buildSkills(config, workRoot, stage) {
  const checkouts = join(workRoot, "skill-sources");
  const projects = join(workRoot, "skill-projects");
  const sources = [];
  const groups = new Set(config.skills.map((source) => source.group));
  for (const group of groups) await mkdir(join(projects, group), { recursive: true });

  for (const source of config.skills) {
    console.log(`\n安装 Skill 来源 ${source.id}@${source.revision}`);
    const checkout = join(checkouts, source.id);
    await cloneSource(source, checkout);
    const args = ["add", checkout];
    for (const name of source.skills) args.push("--skill", name);
    args.push("--agent", "universal", "--copy", "--yes");
    await run(skillsBin, args, {
      cwd: join(projects, source.group),
      env: { ...process.env, CI: "1", DISABLE_TELEMETRY: "1" },
    });
    sources.push({ source, checkout });
  }

  for (const group of groups) {
    const installed = join(projects, group, ".agents", "skills");
    if (!(await exists(installed))) throw new Error(`skills CLI 未生成 ${group} 安装目录`);
    await rename(installed, join(stage, group));
  }

  for (const { source, checkout } of sources) {
    const license = resolve(checkout, source.licensePath);
    const licenseRelative = relative(checkout, license);
    if (
      licenseRelative === ".." ||
      licenseRelative.startsWith(`..${sep}`) ||
      isAbsolute(licenseRelative) ||
      !(await stat(license)).isFile()
    ) {
      throw new Error(`${source.id}.licensePath 无效`);
    }
    const licenses = join(stage, source.group, "licenses");
    await mkdir(licenses, { recursive: true });
    await copyFile(license, join(licenses, `${source.id}-LICENSE`));
    for (const name of source.skills) {
      if (!(await stat(join(stage, source.group, name, "SKILL.md"))).isFile()) {
        throw new Error(`缺少安装后的 Skill：${source.group}/${name}/SKILL.md`);
      }
    }
  }
}

async function buildCli(config, workRoot, stage) {
  const downloads = join(workRoot, "downloads");
  const extracts = join(workRoot, "extracts");
  const binaries = join(stage, "bin");
  const licenses = join(stage, "licenses");
  await mkdir(downloads, { recursive: true });
  await mkdir(extracts, { recursive: true });
  await mkdir(binaries, { recursive: true });
  await mkdir(licenses, { recursive: true });

  for (const tool of config.cli) {
    console.log(`\n安装 CLI ${tool.name}@${tool.version}`);
    const archive = join(downloads, `${tool.name}.download`);
    await download(tool.url, archive);
    const archiveHash = await fileSha256(archive);
    if (archiveHash !== tool.archiveSha256) {
      throw new Error(`${tool.name} archive SHA-256 不匹配：实际 ${archiveHash}`);
    }
    const extracted = join(extracts, tool.name);
    await extract(tool, archive, extracted);
    const matches = await findNamedFiles(extracted, tool.binaryName);
    if (matches.length !== 1) {
      throw new Error(`${tool.name} archive 中找到 ${matches.length} 个 ${tool.binaryName}`);
    }
    const binaryHash = await fileSha256(matches[0]);
    if (binaryHash !== tool.binarySha256) {
      throw new Error(`${tool.name} binary SHA-256 不匹配：实际 ${binaryHash}`);
    }
    const target = join(binaries, tool.installAs);
    await copyFile(matches[0], target);
    await chmod(target, 0o755);
    await download(tool.licenseUrl, join(licenses, `${tool.name}-LICENSE`));
  }
}

async function swapOutputs(workRoot, outputs) {
  const swapped = [];
  try {
    for (const output of outputs) {
      await assertReplaceable(output.target);
      const old = join(workRoot, `old-${output.name}`);
      const hadOld = await exists(output.target);
      swapped.push({ ...output, old, hadOld });
      if (hadOld) await rename(output.target, old);
      await rename(output.stage, output.target);
    }
  } catch (error) {
    const rollbackErrors = [];
    for (const output of swapped.reverse()) {
      try {
        if (await exists(output.target)) {
          await rename(output.target, join(workRoot, `failed-${output.name}`));
        }
        if (output.hadOld && (await exists(output.old))) {
          await rename(output.old, output.target);
        }
      } catch (rollbackError) {
        rollbackErrors.push(rollbackError.message);
      }
    }
    if (rollbackErrors.length) {
      error.message += `\n回滚未完成，保留 ${workRoot}：${rollbackErrors.join("; ")}`;
      error.preserveWorkRoot = true;
    }
    throw error;
  }
}

const [nodeMajor, nodeMinor] = process.versions.node
  .split(".", 2)
  .map((part) => Number.parseInt(part, 10));
if (nodeMajor < 22 || (nodeMajor === 22 && nodeMinor < 20)) {
  throw new Error(`需要 Node.js 22.20 或更高版本，当前为 ${process.versions.node}`);
}

const config = JSON.parse(await readFile(configPath, "utf8"));
validateConfig(config);
if (installSkills) {
  const packageJson = JSON.parse(await readFile(packagePath, "utf8"));
  if (!(await exists(skillsBin))) throw new Error("缺少本地 skills CLI；请先运行 npm ci");
  const expected = packageJson.devDependencies?.skills;
  const actual = (await run(skillsBin, ["--version"], { capture: true })).stdout.trim();
  if (actual !== expected) {
    throw new Error(`skills CLI 版本不匹配：声明 ${expected}，实际 ${actual}`);
  }
}

const workRoot = await mkdtemp(join(repoRoot, ".external-assets-stage-"));
const stageRoot = join(workRoot, "stage");
const outputs = [];
let preserveWorkRoot = false;

try {
  await mkdir(stageRoot, { recursive: true });
  if (installSkills) {
    const stage = join(stageRoot, "skills");
    await mkdir(stage, { recursive: true });
    await buildSkills(config, workRoot, stage);
    outputs.push({ name: "skills", stage, target: skillsTarget });
  }
  if (installCli) {
    const stage = join(stageRoot, "cli");
    await mkdir(stage, { recursive: true });
    await buildCli(config, workRoot, stage);
    outputs.push({ name: "cli", stage, target: cliTarget });
  }
  await swapOutputs(workRoot, outputs);
  console.log("\n外部资产安装完成：");
  for (const output of outputs) console.log(`  ${output.name}: ${output.target}`);
  console.log("生成目录不进入 Git，并由 Docker Compose 只读挂载。");
} catch (error) {
  preserveWorkRoot = error.preserveWorkRoot === true;
  throw error;
} finally {
  if (!preserveWorkRoot) await rm(workRoot, { recursive: true, force: true });
}
