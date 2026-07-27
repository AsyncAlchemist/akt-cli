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
     * on the calendar date of issued_at), IN THE COMPANY DEFAULT CURRENCY.
     *
     * Each ledger leg is stored in its source record's own currency (the ledger
     * table has no currency columns). We convert every leg the way Akaunting's own
     * reports do — per-leg `castDebit()`/`castCredit()` (the DoubleEntry
     * `DefaultCurrency` cast, which divides by each ledgerable's historical
     * `currency_rate`), exactly as `Account::calculateBalance()` sums them. A raw
     * SQL `SUM(debit)` would instead add foreign face values (e.g. ARS + USD) as if
     * they were already base currency — the multi-currency bug this replaces.
     *
     * Orphaned rows — those whose polymorphic `ledgerable` was soft-deleted or
     * removed — are excluded via `whereHasMorph('*')`, honouring each target's
     * SoftDeletes scope, exactly as Akaunting's own reports do.
     *
     * @return \Illuminate\Http\JsonResponse
     */
    public function index(Request $request)
    {
        $query = Ledger::where('company_id', company_id())
            ->whereHasMorph('ledgerable', '*')  // drop orphans (soft-deleted/missing targets)
            ->with('ledgerable')                // the DefaultCurrency cast reads ledgerable->currency_rate
            ->select('id', 'company_id', 'account_id', 'ledgerable_type',
                     'ledgerable_id', 'debit', 'credit', 'issued_at');

        if ($request->filled('date_from')) {
            $query->whereDate('issued_at', '>=', $request->input('date_from'));
        }
        if ($request->filled('date_to')) {
            $query->whereDate('issued_at', '<=', $request->input('date_to'));
        }

        $totals = [];
        $query->chunk(1000, function ($ledgers) use (&$totals) {
            foreach ($ledgers as $ledger) {
                $ledger->castDebit();   // merge the DefaultCurrency cast -> converts on read
                $ledger->castCredit();
                $aid = (int) $ledger->account_id;
                if (! isset($totals[$aid])) {
                    $totals[$aid] = ['debit' => 0.0, 'credit' => 0.0];
                }
                $totals[$aid]['debit']  += (float) $ledger->debit;
                $totals[$aid]['credit'] += (float) $ledger->credit;
            }
        });

        ksort($totals);
        $rows = [];
        foreach ($totals as $aid => $t) {
            $rows[] = (object) [
                'account_id' => $aid,
                'debit'      => round($t['debit'], 4),
                'credit'     => round($t['credit'], 4),
            ];
        }

        return Resource::collection(collect($rows));
    }
}
