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
     * Orphaned rows — those whose polymorphic `ledgerable` (a transaction,
     * document, journal, …) has been soft-deleted or removed — must be excluded,
     * exactly as Akaunting's own reports do; a raw table sum would double-count
     * them. We express that as an EXISTS-per-morph-type filter (`whereHasMorph`
     * with the `*` wildcard), which honors each target's SoftDeletes scope. That
     * yields the same set as hydrating every row and dropping null ledgerables,
     * but resolves to a single GROUP BY aggregate instead of loading thousands
     * of models into PHP on every call.
     *
     * @return \Illuminate\Http\JsonResponse
     */
    public function index(Request $request)
    {
        $query = Ledger::without('ledgerable')      // aggregate rows carry no ledgerable to eager-load
            ->where('company_id', company_id())
            ->whereHasMorph('ledgerable', '*');     // drop orphans in SQL (soft-deleted/missing targets)

        if ($request->filled('date_from')) {
            $query->whereDate('issued_at', '>=', $request->input('date_from'));
        }
        if ($request->filled('date_to')) {
            $query->whereDate('issued_at', '<=', $request->input('date_to'));
        }

        $rows = $query->groupBy('account_id')
            ->orderBy('account_id')
            ->selectRaw('account_id, COALESCE(SUM(debit), 0) as debit, COALESCE(SUM(credit), 0) as credit')
            ->get();

        return Resource::collection($rows);
    }
}
