<?php

namespace Modules\AktApi\Http\Controllers\Api;

use App\Abstracts\Http\ApiController;
use App\Models\Banking\Account;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;

class Banks extends ApiController
{
    /**
     * Gate on DoubleEntry read access (see the Ledgers controller for why we
     * declare our own middleware instead of calling parent::__construct()).
     */
    public function __construct()
    {
        $this->middleware('permission:read-double-entry-chart-of-accounts')->only('unmapped');
    }

    /**
     * Banking accounts (banks) with NO double_entry_account_bank mapping for the
     * current company. DoubleEntry posts a transaction's ledger legs only when its
     * bank is mapped (see the module's Observers/Banking/Transaction::created,
     * which bails when the bank is unmapped), so an unmapped bank silently posts
     * nothing to the general ledger. Powers `akt verify`.
     *
     * This is the one place akt-api reads a DoubleEntry table other than
     * double_entry_ledger (double_entry_account_bank). It does so via the query
     * builder only and imports no DoubleEntry code, so it still survives
     * DoubleEntry updates as long as that table's column names hold.
     *
     * @return \Illuminate\Http\JsonResponse
     */
    public function unmapped(Request $request)
    {
        $mapped = DB::table('double_entry_account_bank')
            ->where('company_id', company_id())
            ->whereNull('deleted_at')
            ->pluck('bank_id')
            ->all();

        $banks = Account::where('company_id', company_id())
            ->when(! empty($mapped), fn ($q) => $q->whereNotIn('id', $mapped))
            ->get(['id', 'name']);

        return response()->json(['data' => $banks->map(fn ($b) => [
            'id' => $b->id,
            'name' => $b->name,
        ])->values()->all()]);
    }
}
