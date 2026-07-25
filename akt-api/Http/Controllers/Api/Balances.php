<?php

namespace Modules\AktApi\Http\Controllers\Api;

use App\Abstracts\Http\ApiController;
use Illuminate\Http\Request;
use Modules\DoubleEntry\Models\Ledger;
use Modules\AktApi\Http\Resources\Balance as Resource;

class Balances extends ApiController
{
    public function __construct()
    {
        // Gate on DoubleEntry read access; do NOT call parent (avoids the base
        // ApiController's auto-assigned permission -> 403). Mirrors Ledgers.
        $this->middleware('permission:read-double-entry-chart-of-accounts')->only('index');
    }

    /**
     * Per-account debit/credit totals over an optional date window (inclusive,
     * on the calendar date of issued_at). One row per account.
     *
     * Aggregates through the DoubleEntry Ledger model's `ledgerable` relation and
     * skips rows whose ledgerable no longer exists (soft-deleted transactions /
     * documents leave orphaned ledger rows behind). This matches how Akaunting's
     * own reports read the ledger; a raw table sum would double-count orphans.
     *
     * @return \Illuminate\Http\JsonResponse
     */
    public function index(Request $request)
    {
        $query = Ledger::where('company_id', company_id());

        if ($request->filled('date_from')) {
            $query->whereDate('issued_at', '>=', $request->input('date_from'));
        }
        if ($request->filled('date_to')) {
            $query->whereDate('issued_at', '<=', $request->input('date_to'));
        }

        $totals = [];
        $query->with('ledgerable')->chunkById(2000, function ($rows) use (&$totals) {
            foreach ($rows as $l) {
                if ($l->ledgerable === null) {
                    continue;   // orphaned (soft-deleted/missing) — excluded, as Akaunting does
                }
                $aid = (int) $l->account_id;
                if (! isset($totals[$aid])) {
                    $totals[$aid] = (object) ['account_id' => $aid, 'debit' => 0.0, 'credit' => 0.0];
                }
                $totals[$aid]->debit += (float) $l->debit;
                $totals[$aid]->credit += (float) $l->credit;
            }
        });

        ksort($totals);

        return Resource::collection(collect(array_values($totals)));
    }
}
