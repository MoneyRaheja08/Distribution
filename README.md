# Ashoka Distribution — Backend API

FastAPI + MongoDB backend for the distribution collection app: dealers with
ageing buckets, stock, and field collections with cheque tracking and cash
reconciliation. Three roles — **admin**, **manager**, **collector**.

## Run it

```bash
cd distribution-backend
python -m venv .venv && source .venv/bin/activate      # optional
pip install -r requirements.txt
cp .env.example .env                                    # then edit .env
uvicorn app.main:app --reload
```

You need a MongoDB running — either local (`mongodb://localhost:27017`) or a
free MongoDB Atlas cluster (put the SRV URI in `.env`).

Interactive API docs: http://localhost:8000/docs

## First-time setup (create the admin)

```bash
# 1) Create the admin (only works once, when there are no users)
curl -X POST localhost:8000/auth/bootstrap \
  -H "Content-Type: application/json" \
  -d '{"name":"Money","pin":"1234"}'
# -> returns { access_token, user }

TOKEN=<paste access_token>

# 2) Add a collector
curl -X POST localhost:8000/users -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Gurpreet","pin":"1111","role":"collector"}'

# 3) Add a dealer with outstanding split into ageing buckets
curl -X POST localhost:8000/dealers -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"Sharma Electronics","area":"Sector 22","credit_limit":300000,
       "collector_id":"<collector id>",
       "ageing":{"age_0_30":98000,"age_31_60":76000,"age_61_90":65500,"age_90p":45000}}'
```

## Login (collector, then collect)

```bash
curl -X POST localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"name":"Gurpreet","pin":"1111"}'

# Record a collection — allocated oldest-first automatically
curl -X POST localhost:8000/payments -H "Authorization: Bearer <collector token>" \
  -H "Content-Type: application/json" \
  -d '{"dealer_id":"<id>","amount":50000,"mode":"Cash"}'
```

## Endpoints

| Method | Path | Who | Notes |
|---|---|---|---|
| POST | `/auth/bootstrap` | anyone (once) | create first admin |
| POST | `/auth/login` | anyone | returns JWT |
| GET/POST | `/users` · PATCH/DELETE `/users/{id}` | admin | manage users |
| GET | `/dealers` | all | collectors see only their own |
| POST/PATCH/DELETE | `/dealers` | admin, manager | feed data |
| GET/POST/PATCH/DELETE | `/stock` | read: all · write: staff | |
| POST | `/payments` | collector/staff | oldest-first allocation, receipt no. |
| GET | `/payments` | all | filters: `dealer_id`, `status`, `day` |
| PATCH | `/payments/{id}/cheque` | staff | `{cleared:true}` clears; `false` bounces + restores outstanding |
| POST | `/payments/deposit` | staff | mark a collector's cash deposited |
| GET | `/payments/summary` | staff | dashboard totals |

## How the money logic works

- **Outstanding** = sum of the four ageing buckets on the dealer.
- **A collection** clears the oldest bucket first (90+ → 61–90 → 31–60 → 0–30),
  records how much came from each bucket, and issues a receipt number.
- **Cash / UPI** post as `cleared`. **Cheques** post as `pending` and only
  count as real money once staff mark them cleared. Marking a cheque **bounced**
  restores exactly what it had cleared, so the dealer's outstanding comes back.
- **Cash-in-hand** = a collector's `cleared`, `Cash`, `deposited:false` payments,
  until staff mark them deposited.

## Notes

- **Timezone:** "today" uses the server's local date. Run the server in IST
  (or set `TZ=Asia/Kolkata`) so a collector's day matches theirs.
- **Transactions:** the record-collection flow writes the dealer then the
  payment sequentially (no multi-doc transaction), which is fine on a single
  MongoDB node. If you move to a replica set / Atlas and want strict atomicity,
  wrap those two writes in a session transaction.
- **PINs** are bcrypt-hashed. Change `JWT_SECRET` in `.env` before going live.
- **Frontend:** point your React (Vite) app at this API. Set `CORS_ORIGINS` to
  your dev URL (`http://localhost:5173`) and later your deployed domain.
