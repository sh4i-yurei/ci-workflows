export default {
  testRunner: 'command',
  commandRunner: { command: 'node --test' },
  reporters: ['clear-text', 'json'],
  mutate: ['src/app.js'],
};
