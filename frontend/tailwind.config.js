/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        serif: ["Georgia", "Cambria", "serif"],
      },
      colors: {
        parchment: "#fffdf7",
        ink: "#1a1a1a",
        sepia: {
          100: "#f5efe0",
          200: "#e8d5a3",
          600: "#8b6914",
          900: "#2c1a0e",
        },
      },
    },
  },
  plugins: [],
};
