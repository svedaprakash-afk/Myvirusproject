import React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, MemoryRouter } from "react-router-dom";
import App from "./App";
import "./styles.css";

const DEMO = !!import.meta.env.VITE_DEMO;
if (DEMO) window.confirm = () => true; // the static demo is sandboxed; confirm() is unavailable there and every mutation is in-memory only
const Router = DEMO ? MemoryRouter : BrowserRouter;

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Router><App />{DEMO && <div className="demo-flag" role="note">Synthetic demo · frozen snapshot · nothing is saved</div>}</Router>
  </React.StrictMode>,
);
