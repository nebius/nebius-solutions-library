import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { ThemeProvider } from "@gravity-ui/uikit";
import "@gravity-ui/uikit/styles/fonts.css";
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@gravity-ui/uikit/styles/styles.css";
import { loadConfig } from "./config";
import "./styles/app.css";
import "./styles/console.css";

loadConfig().then(() => {
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <ThemeProvider theme="light">
        <App />
      </ThemeProvider>
    </React.StrictMode>,
  );
});
