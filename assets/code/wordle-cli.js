#!/usr/bin/env node
'use strict';

/**
 * CLI adapter for https://github.com/ybenhayun/wordlebot
 * Uses the repository's original JavaScript solver, word lists, and rankings.
 * No npm packages, browser, or network connection required.
 *
 * Place next to index.html in the cloned wordlebot repository, or use --repo.
 */

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const readline = require('node:readline');
const { performance } = require('node:perf_hooks');

const HELP = `WordleBot command-line adapter (classic 5 letters / 6 guesses)

Usage:
  node wordle-cli.js                         Interactive session
  node wordle-cli.js SALET:BBYGB            One-shot prediction
  node wordle-cli.js SALET:BBYGB CRANE:GYBBB
  node wordle-cli.js --json SALET:BBYGB      Machine-readable result
  node wordle-cli.js --word SALET:BBYGB      Only print the next guess
  node wordle-cli.js --interactive            Read guesses line by line
  node wordle-cli.js --repo /path/to/wordlebot [guesses...]

Feedback protocol (positions left to right):
  G = green / correct position
  Y = yellow / present, wrong position
  B = black or gray / absent (including extra duplicate letters)
  Also accepts 2=green, 1=yellow, 0=gray.

For each guess, enter WORD:COLORS or WORD COLORS, for example:
  SALET:BBYGB
  CRANE GBBBB

Flags:
  --hard            Hard-mode recommendations (normal mode is default)
  --all             Use repo's 'All Possible Answers' rather than 'Most Likely'
  --json            Print compact JSON (non-interactive)
  --word            Print just the recommended word
  --interactive     Force interactive input (also works with piped lines)
  --repo PATH       Location of cloned repo (default: script directory)
  -h, --help        Show help

Interactive commands: undo, reset, list, help, quit
`;

function parseArgs(argv) {
  const out = { repo: __dirname, hard: false, all: false, json: false, word: false, interactive: false, moves: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--repo') {
      if (!argv[i + 1]) throw new Error('--repo needs a directory path');
      out.repo = path.resolve(argv[++i]);
    } else if (a === '--hard') out.hard = true;
    else if (a === '--all') out.all = true;
    else if (a === '--json') out.json = true;
    else if (a === '--word') out.word = true;
    else if (a === '--interactive') out.interactive = true;
    else if (a === '--help' || a === '-h') out.help = true;
    else if (a.startsWith('--')) throw new Error(`Unknown flag: ${a}`);
    else out.moves.push(a);
  }
  return out;
}

function parseMove(text) {
  const match = /^\s*([a-zA-Z]{5})\s*[:=,\s]\s*([gGyYbBxX012]{5})\s*$/.exec(text);
  if (!match) {
    throw new Error(`Invalid move '${text}'. Use WORD:BYGBB (5 letters, 5 colors).`);
  }
  const word = match[1].toUpperCase();
  const colors = match[2].toUpperCase().replace(/0/g, 'B').replace(/1/g, 'Y')
    .replace(/2/g, 'G').replace(/X/g, 'B');
  return { word, colors };
}

function makeSolver(options) {
  // Load the repo's real browser-era JS in its own context. Only the minimal
  // DOM reads the solver needs are supplied; there is no browser automation.
  let history = [];
  const dom = {
    getElementById(id) {
      if (id === 'max-guesses') return { value: '6' };
      if (id === 'mode') return { checked: options.hard };
      return null;
    },
    getElementsByClassName(name) {
      if (name === 'row') {
        return history.map(({ word, colors }) => ({
          getElementsByClassName(cls) {
            if (cls !== 'tile') return [];
            return [...word].map((char, i) => ({
              innerHTML: char,
              classList: [colors[i]],
            }));
          },
        }));
      }
      return [];
    },
  };
  const ctx = vm.createContext({
    replaceAt: (s, ch, i) =>
        s.slice(0, i) + ch + s.slice(i + 1),

    combineLists: (a, b) =>
        [...new Set([...a, ...b])],

    document: dom,
    performance,
    // Suppress progress/debug logs from the upstream solver on stdout.
    console: { log() {}, warn() {}, error() {} },
  });

  const files = [
    'word_lists/allwords.js',
    'word_lists/commonwords.js',
    'word_lists/officialanswers.js',
    'word_lists/officialguesses.js',
    'word_lists/rankings.js',
    'js/general.js',
    'js/class.js',
    'js/main.js',
    'js/bot.js',
  ];
  for (const filename of files) {
    const fullPath = path.join(options.repo, filename);
    if (!fs.existsSync(fullPath)) {
      throw new Error(`Can't find ${fullPath}\n` +
        'Put wordle-cli.js in the cloned wordlebot directory or specify --repo PATH.');
    }
    vm.runInContext(fs.readFileSync(fullPath, 'utf8'), ctx, {
      filename: fullPath,
    });
  }

  // Don't load setup.js: it initializes the jQuery UI, which doesn't exist
  // in a terminal. Initialize exactly the variables that setup.js normally sets.
  vm.runInContext(`
    var words = [...new Set(big_list.filter(w => w.length === 5))];
    var common = [...new Set(( ${options.all ? 'all_common_words' : 'common_words'} )
      .filter(w => w.length === 5))].sort();
    var bot = new Bot(WORDLE);
    word_length = 5;
    wordbank = '${options.all ? 'complete' : 'restricted'}';
  `, ctx);

  const wordCount = vm.runInContext('words.length', ctx);
  const commonCount = vm.runInContext('common.length', ctx);

  function analyze(moves) {
    history = moves;
    // Different history must not hit cached guesses from an unrelated branch.
    vm.runInContext('seconds = {}; pairings = [];', ctx);
    const used = history.length;
    const basics = { guessesUsed: used, guessesLeft: Math.max(0, 6 - used) };
    if (used && history[used - 1].colors === 'GGGGG') {
      return { ...basics, status: 'solved', recommendation: null, alternatives: [] };
    }
    if (used >= 6) {
      return { ...basics, status: 'out-of-guesses', recommendation: null, alternatives: [] };
    }

    if (!used) {
      const ranked = vm.runInContext('getFirstGuesses()', ctx);
      const candidates = ranked.filter(a => !history.some(m => m.word === a.word)).slice(0, 10);
      return {
        ...basics, status: 'ok', recommendation: candidates[0]?.word ?? null,
        alternatives: candidates.map(a => ({ word: a.word, average: a.average, wrong: a.wrong })),
        likelyCount: commonCount, unlikelyCount: Math.max(0, wordCount - commonCount),
        remaining: null,
      };
    }

    const lists = vm.runInContext('getPotentialGuessesAndAnswers()', ctx);
    const probable = lists.answers.filter(w => !history.some(m => m.word === w));
    const improbable = lists.unlikely.filter(w => !history.some(m => m.word === w));
    const likelyAnswers = [...new Set(probable)].sort();
    const unlikelyAnswers = [...new Set(improbable)].sort();
    if (!probable.length && !improbable.length) {
      return {
        ...basics, status: 'no-matches', recommendation: null,
        alternatives: [], likelyCount: 0, unlikelyCount: 0,
        remaining: [], likelyAnswers: [], unlikelyAnswers: [],
      };
    }

    let ranked;
    if (probable.length <= 2) {
      // The upstream website intentionally shows the remaining answer choices,
      // instead of running the deep solver when 1-2 likely answers remain.
      ranked = probable.map(word => ({ word }));
      if (!ranked.length) ranked = improbable.map(word => ({ word }));
    } else {
      ctx.__lists = lists;
      ranked = vm.runInContext('getBestGuesses(__lists.answers, __lists.guesses, __lists.unique)', ctx);
    }
    ranked = ranked.filter(a => !history.some(m => m.word === a.word)).slice(0, 10);
    return {
      ...basics,
      status: probable.length ? 'ok' : 'unlikely-only',
      recommendation: ranked[0]?.word ?? null,
      alternatives: ranked.map(a => ({ word: a.word, ...(a.average == null ? {} : { average: a.average }),
        ...(a.wrong == null ? {} : { wrong: a.wrong }) })),
      likelyCount: probable.length,
      unlikelyCount: improbable.length,
      remaining: [...likelyAnswers, ...unlikelyAnswers],
      likelyAnswers,
      unlikelyAnswers,
    };
  }

  return { analyze, wordCount, commonCount };
}

function output(result, format = 'normal') {
  if (format === 'word') {
    console.log(result.recommendation || '');
    return;
  }
  if (format === 'json') {
    console.log(JSON.stringify(result));
    return;
  }
  if (result.status === 'solved') {
    console.log('Solved!');
    return;
  }
  if (result.status === 'out-of-guesses') {
    console.log('No guesses left (classic Wordle allows six).');
    return;
  }
  if (result.status === 'no-matches') {
    console.log('No words match that feedback. Double-check your colors, especially duplicates.');
    return;
  }
  console.log(`\nBest guess: ${result.recommendation || '(none)'}`);
  if (result.likelyCount != null) {
    console.log(`Candidates: ${result.likelyCount} likely, ${result.unlikelyCount} unlikely`);
  }
  if (result.alternatives.length > 1) {
    console.log('Alternatives: ' + result.alternatives.slice(1, 5).map(a => a.word).join(', '));
  }
  if (result.likelyAnswers) {
    console.log(`Probable answers (${result.likelyAnswers.length}): ` +
      (result.likelyAnswers.join(', ') || '(none)'));
    console.log(`Unlikely answers (${result.unlikelyAnswers.length}): ` +
      (result.unlikelyAnswers.join(', ') || '(none)'));
  }
  if (result.status === 'unlikely-only') {
    console.log('Note: no likely answers remain in the selected wordbank.');
  }
}

async function interactive(solver) {
  const history = [];
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout, terminal: process.stdin.isTTY });
  console.log('WordleBot CLI | 5 letters | 6 guesses | G=green Y=yellow B=gray');
  console.log('Enter WORD:COLORS, or use undo / reset / list / quit.');
  output(solver.analyze(history));
  const prompt = () => rl.setPrompt(`wordle ${history.length}/6> `) || rl.prompt();
  prompt();
  rl.on('line', line => {
    const cmd = line.trim();
    const lower = cmd.toLowerCase();
    if (lower === 'quit' || lower === 'q' || lower === 'exit') { rl.close(); return; }
    if (lower === 'help' || lower === '?') { console.log(HELP); prompt(); return; }
    if (lower === 'reset') history.length = 0;
    else if (lower === 'undo') history.pop();
    else if (lower === 'list') {
      console.log(history.map((m, i) => `${i + 1}. ${m.word} ${m.colors}`).join('\n') || '(no guesses yet)');
      prompt(); return;
    } else {
      try {
        if (history.length >= 6) throw new Error('Six guesses used. Type reset to start again.');
        if (history.at(-1)?.colors === 'GGGGG') throw new Error('Already solved. Type reset to start again.');
        history.push(parseMove(cmd));
      } catch (err) {
        console.error(err.message);
        prompt(); return;
      }
    }
    try { output(solver.analyze(history)); }
    catch (err) { console.error(`Solver error: ${err.message}`); }
    prompt();
  });
}

async function main() {
  try {
    const options = parseArgs(process.argv.slice(2));
    if (options.help) { console.log(HELP); return; }
    const moves = options.moves.map(parseMove);
    if (moves.length > 6) throw new Error('Classic Wordle allows at most six guesses.');
    const solver = makeSolver(options);
    if (!options.interactive && (moves.length || options.json || options.word || !process.stdin.isTTY)) {
      output(solver.analyze(moves), options.json ? 'json' : (options.word ? 'word' : 'normal'));
    } else {
      await interactive(solver);
    }
  } catch (err) {
    console.error(`Error: ${err.message}`);
    process.exitCode = 1;
  }
}

main();

