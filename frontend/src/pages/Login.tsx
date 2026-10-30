/**
 * Login.
 *
 * The demo accounts are listed on the page deliberately — this is a portfolio
 * project with seeded data, and a reviewer opening it should be able to get in
 * without reading the README. In anything real these would not be here.
 */

import { useState } from "react";

import { useAuth } from "../auth/AuthContext";
import { Field, Spinner } from "../components/common";

const DEMO_ACCOUNTS = [
  { role: "Donor", email: "staff@nandini.veg.restaurant.example", note: "lists surplus food" },
  { role: "Recipient", email: "staff@annapurna.community.kitchen.example", note: "claims donations" },
  { role: "Admin", email: "admin@foodloop.example", note: "safety review queue" },
];

const DEMO_PASSWORD = "foodloop-demo-2026";

export function Login() {
  const { login, loading, error } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    try {
      await login(email, password);
    } catch {
      /* the error is already in context state */
    } finally {
      setSubmitting(false);
    }
  }

  function fillFrom(accountEmail: string) {
    setEmail(accountEmail);
    setPassword(DEMO_PASSWORD);
  }

  return (
    <div className="login-page">
      <div className="login-panel">
        <header className="login-head">
          <h1>FoodLoop</h1>
          <p>
            Surplus food, coordinated. A deterministic safety gate, transactional
            reservations, and impact measured from verified handovers.
          </p>
        </header>

        <form onSubmit={handleSubmit} className="login-form">
          <Field label="Email">
            <input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="username"
              required
            />
          </Field>

          <Field label="Password">
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </Field>

          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}

          <button type="submit" className="btn btn-primary" disabled={submitting || loading}>
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <div className="demo-accounts">
          <h2>Demo accounts</h2>
          <p className="demo-note">
            Seeded by <code>python -m app.cli seed --demo</code>. Click one to fill
            the form.
          </p>
          <ul>
            {DEMO_ACCOUNTS.map((account) => (
              <li key={account.email}>
                <button type="button" onClick={() => fillFrom(account.email)}>
                  <strong>{account.role}</strong>
                  <span className="demo-email">{account.email}</span>
                  <span className="demo-hint">{account.note}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>

        {loading && <Spinner label="Checking your session" />}
      </div>
    </div>
  );
}
