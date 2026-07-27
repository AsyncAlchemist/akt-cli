<?php

namespace Modules\AktApi\Http\Controllers\Api;

use App\Abstracts\Http\ApiController;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;

class Meta extends ApiController
{
    public function __construct()
    {
        // Read-only DoubleEntry metadata; same gating pattern as Ledgers/Balances
        // (no parent::__construct, which would auto-require a nonexistent permission).
        $this->middleware('permission:read-double-entry-chart-of-accounts')->only('accountTypes');
    }

    /**
     * The DoubleEntry account-type -> class map (1 Assets, 2 Liabilities,
     * 3 Expenses, 4 Income, 5 Equity), read straight from the installation so the
     * CLI never hardcodes it. `double_entry_types` is a global seed (no company_id),
     * so a custom account type the user added still resolves to its class here.
     *
     * @return \Illuminate\Http\JsonResponse
     */
    public function accountTypes(Request $request)
    {
        $rows = DB::table('double_entry_types as t')
            ->leftJoin('double_entry_classes as c', 'c.id', '=', 't.class_id')
            ->whereNull('t.deleted_at')
            ->orderBy('t.id')
            ->get(['t.id as type_id', 't.class_id', 'c.name as class_name']);

        return response()->json(['data' => $rows]);
    }
}
