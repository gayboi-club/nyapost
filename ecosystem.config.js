module.exports = {
  apps: [
    {
      name: "nyapost-web",
      script: "app.py",
      interpreter: "python",
      cwd: __dirname,
      env: {
        FLASK_DEBUG: "0",
      },
    },
    {
      name: "nyapost-bot",
      script: "bot.py",
      interpreter: "python",
      cwd: __dirname,
      env: {
        FLASK_DEBUG: "0",
      },
    },
  ],
};
