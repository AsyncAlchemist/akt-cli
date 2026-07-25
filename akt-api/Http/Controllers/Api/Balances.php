<?php

namespace Modules\AktApi\Http\Controllers\Api;

use App\Abstracts\Http\ApiController;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
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
     * Per-account debit/credit totals over an optional date window
     * (inclusive, on the calendar date of issued_at). One row per account.
     *
     * @return \Illuminate\Http\JsonResponse
     */
    public function index(Request $request)
    {
        $query = DB::table('double_entry_ledger')
            ->where('company_id', company_id());

        if ($request->filled('date_from')) {
            $query->whereDate('issued_at', '>=', $request->input('date_from'));
        }
        if ($request->filled('date_to')) {
            $query->whereDate('issued_at', '<=', $request->input('date_to'));
        }

        $rows = $query->groupBy('account_id')
            ->selectRaw('account_id, COALESCE(SUM(debit),0) as debit, COALESCE(SUM(credit),0) as credit')
            ->orderBy('account_id')
            ->get();

        return Resource::collection($rows);
    }
}
