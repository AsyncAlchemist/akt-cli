<?php

namespace Modules\AktApi\Http\Controllers\Api;

use App\Abstracts\Http\ApiController;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Modules\DoubleEntry\Models\Ledger as DeLedger;
use Modules\AktApi\Http\Resources\Ledger as Resource;

class Ledgers extends ApiController
{
    /**
     * Gate on DoubleEntry read access — the data this exposes is DoubleEntry's,
     * and the admin/API role already holds this permission.
     *
     * We intentionally do NOT call parent::__construct(): the base
     * ApiController::__construct() runs assignPermissionsToController(), which
     * would auto-require a non-existent `read-akt-api-ledgers` permission and
     * return 403. Declaring our own middleware here (mirroring the DoubleEntry
     * module's own API controllers) is the supported pattern.
     */
    public function __construct()
    {
        $this->middleware('permission:read-double-entry-chart-of-accounts')->only('index', 'orphans');
        $this->middleware('permission:update-double-entry-chart-of-accounts')->only('update', 'pruneOrphans', 'split');
    }

    /**
     * Report orphaned ledger rows, grouped by ledgerable_type. An orphan is a
     * row the DoubleEntry Ledger model no longer returns (its ledgerable — the
     * transaction/document/journal — was soft-deleted or removed), so we diff the
     * raw table against the live ids the model yields.
     *
     * @return \Illuminate\Http\JsonResponse
     */
    public function orphans(Request $request)
    {
        $live = DeLedger::where('company_id', company_id())->pluck('id')->all();

        $by_type = DB::table('double_entry_ledger')
            ->where('company_id', company_id())
            ->when(! empty($live), fn ($q) => $q->whereNotIn('id', $live))
            ->groupBy('ledgerable_type')
            ->selectRaw('ledgerable_type, count(*) as c')
            ->pluck('c', 'ledgerable_type')
            ->all();

        return response()->json(['data' => ['total' => array_sum($by_type), 'by_type' => $by_type]]);
    }

    /**
     * Hard-delete orphaned ledger rows (see orphans()). These rows are already
     * dead — Akaunting's reports ignore them — so removing them is safe cleanup.
     *
     * @return \Illuminate\Http\JsonResponse
     */
    public function pruneOrphans(Request $request)
    {
        $live = DeLedger::where('company_id', company_id())->pluck('id')->all();

        $orphan_ids = DB::table('double_entry_ledger')
            ->where('company_id', company_id())
            ->when(! empty($live), fn ($q) => $q->whereNotIn('id', $live))
            ->pluck('id')
            ->all();

        $deleted = 0;
        foreach (array_chunk($orphan_ids, 500) as $batch) {
            $deleted += DB::table('double_entry_ledger')->whereIn('id', $batch)->delete();
        }

        return response()->json(['data' => ['deleted' => $deleted]]);
    }

    /**
     * Display a listing of general-ledger postings.
     *
     * Reads DoubleEntry's ledger table directly (no DoubleEntry class imported).
     * The table name is singular: double_entry_ledger.
     *
     * @return \Illuminate\Http\JsonResponse
     */
    public function index(Request $request)
    {
        // Opt-in (?convert=1): show each leg AS POSTED (its own currency) plus
        // base-currency columns. Convert through DoubleEntry's own DefaultCurrency
        // cast — the exact per-leg mechanism Account::calculateBalance uses — so
        // invoice/bill/tax/transfer legs, whose rate lives on a parent record
        // rather than the leg itself, convert correctly (a flat table lookup can't).
        if ($request->boolean('convert')) {
            return $this->indexConverted($request);
        }

        $query = $this->applyLedgerFilters(
            DB::table('double_entry_ledger')->where('company_id', company_id()), $request);

        $rows = $query->orderBy('issued_at')->orderBy('id')
            ->paginate((int) $request->input('limit', 100));

        return Resource::collection($rows);
    }

    /** Apply the shared account_id/ledgerable/type/date filters to a builder. */
    private function applyLedgerFilters($query, Request $request)
    {
        foreach (['account_id', 'ledgerable_id'] as $intField) {
            if ($request->filled($intField)) {
                $query->where($intField, (int) $request->input($intField));
            }
        }
        foreach (['ledgerable_type', 'entry_type'] as $strField) {
            if ($request->filled($strField)) {
                $query->where($strField, $request->input($strField));
            }
        }
        if ($request->filled('issued_from')) {
            $query->where('issued_at', '>=', $request->input('issued_from'));
        }
        if ($request->filled('issued_to')) {
            $query->where('issued_at', '<=', $request->input('issued_to'));
        }
        return $query;
    }

    /**
     * ?convert=1 path: hydrate via the Eloquent Ledger model + `with('ledgerable')`
     * and apply castDebit()/castCredit() (the DefaultCurrency cast) for the base
     * amounts — correct for every leg type, including document/tax legs whose rate
     * is on the parent document. Raw debit/credit stay as posted.
     */
    private function indexConverted(Request $request)
    {
        $query = $this->applyLedgerFilters(
            DeLedger::where('company_id', company_id())->with('ledgerable'), $request);

        $rows = $query->orderBy('issued_at')->orderBy('id')
            ->paginate((int) $request->input('limit', 100));

        $rows->getCollection()->transform(function ($l) {
            $rawDebit  = $l->getRawOriginal('debit');
            $rawCredit = $l->getRawOriginal('credit');
            $currency  = $this->legCurrency($l->ledgerable);
            $l->castDebit();   // merge the DefaultCurrency cast -> converts on read
            $l->castCredit();
            return (object) [
                'id'               => $l->id,
                'company_id'       => $l->company_id,
                'account_id'       => $l->account_id,
                'ledgerable_type'  => $l->ledgerable_type,
                'ledgerable_id'    => $l->ledgerable_id,
                'entry_type'       => $l->entry_type,
                'issued_at'        => $l->issued_at,
                'debit'            => $rawDebit,
                'credit'           => $rawCredit,
                'currency_code'    => $currency,
                'debit_converted'  => $rawDebit  === null ? null : round((float) $l->debit, 4),
                'credit_converted' => $rawCredit === null ? null : round((float) $l->credit, 4),
            ];
        });

        return Resource::collection($rows);
    }

    /**
     * Best-effort source currency of a leg: the transaction/journal's own currency,
     * or a document line's parent-document currency; falls back to the default.
     * (Display label only — the converted amounts above come from the cast.)
     */
    private function legCurrency($ledgerable): string
    {
        if (! $ledgerable) {
            return default_currency();
        }
        if (! empty($ledgerable->currency_code)) {
            return $ledgerable->currency_code;
        }
        if (! empty($ledgerable->document) && ! empty($ledgerable->document->currency_code)) {
            return $ledgerable->document->currency_code;
        }
        return default_currency();
    }

    /**
     * Repoint a single item-leg ledger posting to a different GL account.
     *
     * Only the `account_id` changes — debit/credit amounts and the paired
     * total-leg (bank) row are untouched, so the entry stays balanced. Restricted
     * to `entry_type = 'item'` rows so the bank leg can never be moved, and
     * company-scoped both on read and write.
     *
     * @param  int|string  $id
     * @return \Illuminate\Http\JsonResponse
     */
    public function update(Request $request, $id)
    {
        $request->validate(['account_id' => 'required|integer']);

        $row = DB::table('double_entry_ledger')
            ->where('id', (int) $id)
            ->where('company_id', company_id())
            ->first();

        if (! $row) {
            return $this->errorInternal('ledger row ' . $id . ' not found');
        }
        if ($row->entry_type !== 'item') {
            return $this->errorInternal("only item-leg rows may be recoded (row {$id} is '{$row->entry_type}')");
        }

        DB::table('double_entry_ledger')
            ->where('id', (int) $id)
            ->where('company_id', company_id())
            ->update([
                'account_id' => (int) $request->input('account_id'),
                'updated_at' => now(),
            ]);

        return new Resource((object) array_merge(
            (array) $row,
            ['account_id' => (int) $request->input('account_id')]
        ));
    }

    /**
     * Split one item-leg posting into N item legs (a multi-GL "split" transaction).
     *
     * DoubleEntry posts a standalone transaction as exactly one item leg
     * (`de_account_id`) + one total (bank) leg — a bank transaction can't natively
     * carry multiple GL accounts the way an invoice's line items do. This fans the
     * single item leg out into N item legs by writing directly to the ledger table
     * (same table + query-builder discipline as `update()` above), so one bank
     * transaction can post to several GL accounts.
     *
     * The `total` (bank) leg is never touched; the new legs MUST net to the original
     * item leg's `(debit - credit)` so the entry stays balanced. Each new row is
     * templated off the existing row, so every DoubleEntry column (created_from,
     * created_by, ledgerable_*, issued_at, entry_type, …) is reproduced exactly —
     * only account_id/debit/credit are overridden. Restricted to `entry_type = 'item'`
     * and refuses a transaction that is already split; company-scoped throughout.
     *
     * @param  int|string  $id  the id of the (single) item-leg row to split
     * @return \Illuminate\Http\JsonResponse
     */
    public function split(Request $request, $id)
    {
        $request->validate([
            'legs'              => 'required|array|min:1',
            'legs.*.account_id' => 'required|integer',
            'legs.*.debit'      => 'nullable|numeric',
            'legs.*.credit'     => 'nullable|numeric',
        ]);

        $row = DB::table('double_entry_ledger')
            ->where('id', (int) $id)
            ->where('company_id', company_id())
            ->first();

        if (! $row) {
            return $this->errorInternal('ledger row ' . $id . ' not found');
        }
        if ($row->entry_type !== 'item') {
            return $this->errorInternal("only item-leg rows may be split (row {$id} is '{$row->entry_type}')");
        }

        // Refuse if this transaction already has more than one item leg (already
        // split) — keeps the operation unambiguous and safe to retry.
        $itemLegCount = DB::table('double_entry_ledger')
            ->where('company_id', company_id())
            ->where('ledgerable_type', $row->ledgerable_type)
            ->where('ledgerable_id', $row->ledgerable_id)
            ->where('entry_type', 'item')
            ->count();
        if ($itemLegCount !== 1) {
            return $this->errorInternal("transaction already has {$itemLegCount} item legs; refusing to split (row {$id})");
        }

        // Balance rule: the new legs' net (debit - credit) must equal the row's, so
        // the untouched total (bank) leg keeps the whole entry balanced.
        $legs = $request->input('legs');
        $sumDebit = 0.0;
        $sumCredit = 0.0;
        foreach ($legs as $leg) {
            $sumDebit  += (float) ($leg['debit'] ?? 0);
            $sumCredit += (float) ($leg['credit'] ?? 0);
        }
        $newNet = round($sumDebit - $sumCredit, 4);
        $oldNet = round((float) $row->debit - (float) $row->credit, 4);
        if (abs($newNet - $oldNet) > 0.0001) {
            return $this->errorInternal("split legs net {$newNet} != item-leg net {$oldNet}; would unbalance the entry");
        }

        $inserted = [];
        DB::transaction(function () use ($row, $legs, &$inserted) {
            $template = (array) $row;
            unset($template['id']);
            $now = now();
            foreach ($legs as $leg) {
                $debit  = (float) ($leg['debit'] ?? 0);
                $credit = (float) ($leg['credit'] ?? 0);
                $newRow = array_merge($template, [
                    'account_id' => (int) $leg['account_id'],
                    'debit'      => $debit > 0 ? $debit : null,
                    'credit'     => $credit > 0 ? $credit : null,
                    'created_at' => $now,
                    'updated_at' => $now,
                ]);
                $newRow['id'] = DB::table('double_entry_ledger')->insertGetId($newRow);
                $inserted[] = (object) $newRow;
            }
            DB::table('double_entry_ledger')
                ->where('id', $row->id)
                ->where('company_id', company_id())
                ->delete();
        });

        return Resource::collection(collect($inserted));
    }
}
