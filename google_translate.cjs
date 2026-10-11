'use strict';

const translate = require('google-translate-api');

async function main() {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  const payload = JSON.parse(input || '{}');
  const text = String(payload.text || '').trim();
  const target = String(payload.target || '').trim();
  if (!text || !target) throw new Error('text and target are required');

  const result = await translate(text, { to: target });
  const translation = result && typeof result.text === 'string' ? result.text.trim() : '';
  if (!translation) throw new Error('Google Translate returned no text');
  process.stdout.write(JSON.stringify({ translation }));
}

main().catch(error => {
  // Do not log the user's message.
  process.stderr.write(String(error && error.message ? error.message : error).slice(0, 800) + '\n');
  process.exitCode = 1;
});
