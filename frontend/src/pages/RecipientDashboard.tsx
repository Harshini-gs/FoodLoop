/**
 * Recipient dashboard.
 *
 * Two things worth noticing in the code below:
 *
 * 1. A 409 from `reserve` is handled as a NORMAL outcome, not an error. It
 *    means another recipient won the race, so the UI says so plainly and
 *    refreshes the list. The alternative — an alarming red error — would
 *    misrepresent what happened.
 *
 * 2. Only the donor's AREA is shown. The exact address arrives with the
 *    pickup assignment, once a handover is actually agreed. A recipient
 *    browsing the board has no need for a restaurant's precise location.
 */

import { useCallback, useEffect, useState } from "react";

import { ApiRequestError, donations, handover, matching } from "../api/client";
import type { AvailableDonation, Reservation } from "../api/types";
import {
  Card,
  EmptyState,
  ErrorBox,
  Field,
  ProvenanceTag,
  Spinner,
  StatusPill,
  formatDateTime,
  formatQuantity,
  formatTimeOnly,
} from "../components/common";

type CategoryFilter = "ALL" | string;

export function RecipientDashboard() {
  const [available, setAvailable] = useState<AvailableDonation[] | null>(null);
  const [reservations, setReservations] = useState<Reservation[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [category, setCategory] = useState<CategoryFilter>("ALL");
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [offers, mine] = await Promise.all([
        donations.available(),
        matching.mine(),
      ]);
      setAvailable(offers);
      setReservations(mine);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiRequestError ? err.message : "could not load offers");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const categories = Array.from(
    new Set((available ?? []).map((d) => d.food_category)),
  );

  const visible = (available ?? []).filter(
    (d) => category === "ALL" || d.food_category === category,
  );

  async function claim(donation: AvailableDonation) {
    const remaining =
      Number(donation.quantity_total) - Number(donation.quantity_reserved);
    try {
      const reservation = await matching.reserve(donation.id, String(remaining));
      await matching.confirm(reservation.id);
      setNotice(`Claimed ${remaining} ${donation.unit.toLowerCase()} from ${donation.donor_name}.`);
      await refresh();
    } catch (err) {
      if (err instanceof ApiRequestError && err.isConflict) {
        // Not an error. Someone else got there first.
        setNotice(
          `Another recipient claimed that quantity first — the list has been refreshed.`,
        );
        await refresh();
        return;
      }
      setNotice(err instanceof ApiRequestError ? err.message : "could not claim");
    }
  }

  return (
    <div className="page">
      <Card
        title="Available now"
        subtitle="Only donations that have cleared the safety gate appear here."
        actions={
          <button type="button" className="btn btn-sm" onClick={() => void refresh()}>
            Refresh
          </button>
        }
      >
        {notice && (
          <div className="box box-info">
            <p>{notice}</p>
          </div>
        )}

        {categories.length > 1 && (
          <div className="filters">
            <button
              type="button"
              className={`chip ${category === "ALL" ? "chip-on" : ""}`}
              onClick={() => setCategory("ALL")}
            >
              All
            </button>
            {categories.map((c) => (
              <button
                key={c}
                type="button"
                className={`chip ${category === c ? "chip-on" : ""}`}
                onClick={() => setCategory(c)}
              >
                {c.replace(/_/g, " ").toLowerCase()}
              </button>
            ))}
          </div>
        )}

        {error && <ErrorBox message={error} onRetry={() => void refresh()} />}
        {!error && available === null && <Spinner label="Loading offers" />}
        {!error && available !== null && visible.length === 0 && (
          <EmptyState
            title="Nothing available right now"
            hint="Offers appear once a donor lists food and it clears the safety check."
          />
        )}

        {visible.length > 0 && (
          <table className="offers">
            <thead>
              <tr>
                <th>Food</th>
                <th>Donor</th>
                <th>Available</th>
                <th>Pickup window</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {visible.map((donation) => {
                const remaining =
                  Number(donation.quantity_total) - Number(donation.quantity_reserved);
                return (
                  <tr key={donation.id}>
                    <td>
                      <strong>{donation.title}</strong>
                      <span className="small">
                        {donation.food_category.replace(/_/g, " ").toLowerCase()}
                      </span>
                    </td>
                    <td>
                      {donation.donor_name}
                      <span className="small">
                        {donation.donor_area?.trim() || "area not given"}
                      </span>
                    </td>
                    <td className="num">
                      {formatQuantity(String(remaining), donation.unit)}
                      {Number(donation.quantity_reserved) > 0 && (
                        <span className="small">
                          {donation.quantity_reserved} already reserved
                        </span>
                      )}
                    </td>
                    <td>
                      {formatTimeOnly(donation.pickup_window_start)} –{" "}
                      {formatTimeOnly(donation.pickup_window_end)}
                      <span className="small">
                        {formatDateTime(donation.pickup_window_start).split(",")[0]}
                      </span>
                    </td>
                    <td>
                      <div className="cell-actions">
                        <ProvenanceTag source={donation.data_source} />
                        <button
                          type="button"
                          className="btn btn-primary btn-sm"
                          onClick={() => void claim(donation)}
                          disabled={remaining <= 0}
                        >
                          Claim
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}

        <p className="small">
          Exact pickup addresses are released with the pickup assignment, not
          while browsing.
        </p>
      </Card>

      <Card title="Your reservations">
        {reservations === null && <Spinner label="Loading reservations" />}
        {reservations !== null && reservations.length === 0 && (
          <EmptyState title="No reservations yet" />
        )}
        {reservations !== null && reservations.length > 0 && (
          <div className="reservation-list">
            {reservations.map((reservation) => (
              <ReservationRow
                key={reservation.id}
                reservation={reservation}
                onChanged={() => void refresh()}
              />
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

/** A pickup in one of these states has nothing left to confirm. */
const PICKUP_RESOLVED = new Set(["DELIVERED", "CANCELLED", "FAILED"]);

function ReservationRow({
  reservation,
  onChanged,
}: {
  reservation: Reservation;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [pickupId, setPickupId] = useState<string | null>(null);
  const [handoverCode, setHandoverCode] = useState<string | null>(null);
  const [recovered, setRecovered] = useState(false);
  const [code, setCode] = useState("");
  const [quantity, setQuantity] = useState(reservation.quantity);

  const isLive =
    reservation.status === "HELD" || reservation.status === "CONFIRMED";

  // Recover a pickup assigned in an earlier session.
  //
  // The pickup id used to live only in this component's state, set when the
  // user pressed "Assign volunteer". Signing out or closing the tab therefore
  // destroyed the only route to "Confirm receipt" and stranded the
  // reservation — the volunteer could collect the food and nobody could ever
  // record that it arrived. The id is not a secret; the CODES are, and those
  // stay unrecoverable because only their hashes are stored.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const pickup = await handover.forReservation(reservation.id);
        if (cancelled || pickup === null) return;
        if (PICKUP_RESOLVED.has(pickup.status)) return;
        setPickupId(pickup.id);
        setRecovered(true);
      } catch {
        // No pickup yet, or this user is not a party to it. Neither is an
        // error worth putting on screen.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reservation.id]);

  async function act(fn: () => Promise<void>) {
    setBusy(true);
    setStatus(null);
    try {
      await fn();
    } catch (err) {
      setStatus(err instanceof ApiRequestError ? err.message : "failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="reservation">
      <div className="reservation-head">
        <div>
          <strong>{formatQuantity(reservation.quantity)}</strong>
          <span className="small">
            {reservation.status === "HELD"
              ? `hold expires ${formatDateTime(reservation.expires_at)}`
              : `created ${formatDateTime(reservation.created_at)}`}
          </span>
        </div>
        <StatusPill status={reservation.status} />
      </div>

      {isLive && (
        <div className="row">
          {reservation.status === "HELD" && (
            <button
              type="button"
              className="btn btn-sm btn-primary"
              disabled={busy}
              onClick={() =>
                act(async () => {
                  await matching.confirm(reservation.id);
                  onChanged();
                })
              }
            >
              Accept
            </button>
          )}
          <button
            type="button"
            className="btn btn-sm"
            disabled={busy}
            onClick={() =>
              act(async () => {
                await matching.decline(reservation.id);
                onChanged();
              })
            }
          >
            Decline
          </button>
          {/* Gone once a pickup is live: pressing it again would issue a new
              code and silently invalidate the one the donor is holding. */}
          {pickupId === null && (
            <button
              type="button"
              className="btn btn-sm"
              disabled={busy}
              onClick={() =>
                act(async () => {
                  const pickup = await handover.assign(reservation.id);
                  setPickupId(pickup.id);
                  setRecovered(false);
                  // The plaintext handover code is returned exactly once, here.
                  // Only its hash is stored, so if it is not shown now it
                  // cannot be recovered and the pickup must be reassigned.
                  setHandoverCode(pickup.handover_code);
                  setStatus(
                    `Volunteer assigned · ${pickup.estimated_distance_km} km (${pickup.data_source.toLowerCase()})`,
                  );
                })
              }
            >
              Assign volunteer
            </button>
          )}
        </div>
      )}

      {handoverCode && (
        <div className="box box-info">
          <strong>Collection code: {handoverCode}</strong>
          <p className="small">
            Give this to the donor — they enter it to confirm the volunteer
            collected the food. Shown once; only a hash is stored, so if it is
            lost the pickup must be reassigned.
          </p>
        </div>
      )}

      {pickupId && (
        <details className="collect" open>
          <summary>Confirm receipt</summary>
          <div className="collect-body">
            {recovered && (
              <p className="small">
                A volunteer is already assigned for this reservation. The
                collection code was shown once, at assignment, and cannot be
                retrieved — the donor has it.
              </p>
            )}
            <Field label="Delivery code">
              <input
                id={`dcode-${reservation.id}`}
                value={code}
                onChange={(e) => setCode(e.target.value.toUpperCase())}
                maxLength={8}
              />
            </Field>
            <Field label="Quantity actually received">
              <input
                id={`dqty-${reservation.id}`}
                type="number"
                step="0.01"
                value={quantity}
                onChange={(e) => setQuantity(e.target.value)}
              />
            </Field>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={busy || code.length < 4}
              onClick={() =>
                act(async () => {
                  await handover.deliver(pickupId, code, quantity);
                  setStatus("Receipt confirmed.");
                  onChanged();
                })
              }
            >
              Record delivery
            </button>
            <p className="small">
              The delivery code is issued at collection. Only a verified code
              produces a delivery record, which is why no automated process can
              close a donation.
            </p>
          </div>
        </details>
      )}

      {status && <p className="small">{status}</p>}
    </article>
  );
}
