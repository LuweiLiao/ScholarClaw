#!/usr/bin/env node
/**
 * Auto-detect Python 3.10+ and install backend dependencies.
 * Called by `npm postinstall` or `scholarlab setup`.
 */

import { execSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');

function findSystemPython() {
  for (const cmd of ['python3', 'python']) {
    try {
      const ver = execSync(`${cmd} --version`, { encoding: 'utf8' }).trim();
      const match = ver.match(/Python (\d+)\.(\d+)/);
      if (match) {
        const [, major, minor] = match.map(Number);
        if (major >= 3 && minor >= 10) {
          console.log(`  Found ${ver} at: ${cmd}`);
          return cmd;
        }
      }
    } catch { /* not found */ }
  }
  return null;
}

export async function setupPython() {
  console.log('\n  ScholarLab — Python Environment Setup\n');

  const venvDir = join(ROOT, '.venv');
  const isWin = process.platform === 'win32';
  const venvPython = isWin
    ? join(venvDir, 'Scripts', 'python.exe')
    : join(venvDir, 'bin', 'python');

  if (existsSync(venvPython)) {
    console.log('  [ok] Virtual environment already exists at .venv/');
    console.log('  Updating dependencies...\n');
    try {
      execSync(`"${venvPython}" -m pip install -r "${join(ROOT, 'requirements.txt')}" --quiet`, {
        stdio: 'inherit', cwd: ROOT,
      });
      console.log('\n  [ok] Dependencies up to date.\n');
    } catch (e) {
      console.error('\n  [warn] pip install failed — you may need to install manually.\n');
    }
    return;
  }

  const sysPy = findSystemPython();
  if (!sysPy) {
    console.error('  [error] Python 3.10+ not found. Please install Python first:');
    console.error('          https://www.python.org/downloads/\n');
    return;
  }

  console.log('  Creating virtual environment at .venv/ ...');
  try {
    execSync(`${sysPy} -m venv "${venvDir}"`, { stdio: 'inherit', cwd: ROOT });
    console.log('  [ok] Virtual environment created.\n');
  } catch {
    console.error('  [error] Failed to create venv. Please create it manually:\n');
    console.error(`          ${sysPy} -m venv .venv\n`);
    return;
  }

  console.log('  Installing dependencies...\n');
  try {
    execSync(`"${venvPython}" -m pip install -r "${join(ROOT, 'requirements.txt')}" --quiet`, {
      stdio: 'inherit', cwd: ROOT,
    });
    console.log('\n  [ok] Dependencies installed.\n');
  } catch {
    console.error('\n  [warn] pip install failed. Run manually:\n');
    console.error(`          "${venvPython}" -m pip install -r requirements.txt\n`);
  }

  // Install the researchclaw agent package
  const agentDir = join(ROOT, 'backend', 'agent');
  if (existsSync(join(agentDir, 'setup.py')) || existsSync(join(agentDir, 'pyproject.toml'))) {
    console.log('  Installing ScholarLab agent package...\n');
    try {
      execSync(`"${venvPython}" -m pip install -e "${agentDir}[all]" --quiet`, {
        stdio: 'inherit', cwd: ROOT,
      });
      console.log('\n  [ok] Agent package installed.\n');
    } catch {
      console.error('\n  [warn] Agent package install failed. Run manually:\n');
      console.error(`          cd backend/agent && pip install -e ".[all]"\n`);
    }
  }

  console.log('  Setup complete. Run: scholarlab start\n');
}

// Direct invocation
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  setupPython();
}
