export default {
  testRunner: 'command',
  commandRunner: { command: 'npm run test:covered' },
  mutate: ['covered/src/app.js'],
  reporters: ['clear-text', 'json'],
  jsonReporter: { fileName: 'reports/mutation/mutation.json' },
  thresholds: { high: 100, low: 100, break: 0 },
};
