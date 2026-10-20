/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        abyss: "#202522",
        deep: "#F5F4EF",
        surface: "#FFFFFF",
        surface2: "#ECEEE8",
        line: "#D8DDD5",
        foam: "#252C28",
        mist: "#606960",
        teal: "#AF4321",
        cyan: "#477486",
        seablue: "#536FA3",
        kelp: "#277253",
        coral: "#B54747",
        amber: "#96631B",
      },
      fontFamily: {
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Helvetica Neue",
          "Arial",
          "sans-serif",
        ],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      borderRadius: { sm: "0.125rem" },
      boxShadow: {
        sm: "0 1px 2px 0 rgb(0 0 0 / 0.05)",
        depth: "0 2px 5px rgba(30,40,30,0.025)",
      },
    },
  },
  plugins: [],
};
