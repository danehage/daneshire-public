/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  // The app is light-only. Class strategy keeps OS-level dark preference from
  // activating dark: variants against the always-cream background.
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        cream: "var(--cream)",
        "warm-white": "var(--warm-white)",
        ink: "var(--ink)",
        "dark-brown": "var(--dark-brown)",
        "mid-brown": "var(--mid-brown)",
        "light-brown": "var(--light-brown)",
        accent: "var(--accent)",
        "accent-hover": "var(--accent-hover)",
        "warm-tan": "var(--warm-tan)",
        error: "var(--error)",
        warning: "var(--warning)",
        success: "var(--success)",
        gain: "var(--gain)",
        loss: "var(--loss)",
      },
      fontFamily: {
        serif: ["Georgia", "Times New Roman", "serif"],
        sans: ["-apple-system", "BlinkMacSystemFont", "Segoe UI", "system-ui", "sans-serif"],
      },
      borderWidth: {
        "3": "3px",
      },
    },
  },
  plugins: [require("@tailwindcss/typography")],
};
