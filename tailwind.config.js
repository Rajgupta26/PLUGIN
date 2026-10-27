// Tailwind settings for styles.css, the stylesheet of index.html and share.html.
// After changing classes in either page, rebuild it:
//   npx tailwindcss@3.4.5 -i styles.src.css -o styles.css --minify
module.exports = {
  content: ['./index.html', './share.html'],
  theme: {
    extend: {
      colors: {
        ivory: '#FBF9F5',
        ink: '#181715',
        gold: { DEFAULT: '#C8A152', light: '#E6CE93', deep: '#A9843B' },
        line: '#EADEC9',
      },
      fontFamily: {
        serif: ['"Playfair Display"', 'serif'],
        sans: ['Inter', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
      },
    },
  },
};
