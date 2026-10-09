import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { loadConfig } from "./config";
import { ThemeProvider } from "@gravity-ui/uikit";
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@gravity-ui/uikit/styles/styles.css";
import "./styles/app.css";

loadConfig().then(() => {
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <ThemeProvider theme="light">
        <App />
      </ThemeProvider>
    </React.StrictMode>,
  );
});
