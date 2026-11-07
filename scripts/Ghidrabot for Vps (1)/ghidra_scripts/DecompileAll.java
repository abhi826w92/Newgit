import java.io.FileWriter;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicInteger;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressIterator;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.DataIterator;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionManager;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolIterator;
import ghidra.program.model.symbol.SymbolType;

public class DecompileAll extends GhidraScript {

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        String cFile = args.length > 0 ? args[0] : "decompiled.c";
        String metaFile = args.length > 1 ? args[1] : "info.txt";

        // Optional wall-clock budget (seconds). When reached, stop cleanly and
        // leave the functions already written on disk as partial output.
        long budgetMs = 0;
        if (args.length > 2) {
            try {
                budgetMs = Math.max(0, Long.parseLong(args[2].trim())) * 1000L;
            } catch (NumberFormatException ignored) {}
        }
        long startMs = System.currentTimeMillis();

        PrintWriter meta = new PrintWriter(new FileWriter(metaFile));
        meta.println("=== FILE INFO ===");
        meta.println("File       : " + currentProgram.getName());
        meta.println("Language   : " + currentProgram.getLanguage().getLanguageID());
        meta.println("Compiler   : " + currentProgram.getCompilerSpec().getCompilerSpecID());
        meta.println("Processor  : " + currentProgram.getLanguage().getProcessor());
        meta.println("");

        List<String> strings = new ArrayList<>();
        DataIterator dataIt = currentProgram.getListing().getDefinedData(true);
        while (dataIt.hasNext() && strings.size() < 20000) {
            Data d = dataIt.next();
            if (d.hasStringValue() && d.getValue() instanceof String) {
                String s = (String) d.getValue();
                if (s.length() >= 4) {
                    strings.add(s);
                }
            }
        }
        meta.println("=== STRINGS (" + strings.size() + ") ===");
        for (String s : strings) {
            meta.println(s.replace("\n", "\\n").replace("\r", "\\r"));
        }
        meta.println("");

        List<String> symbols = new ArrayList<>();
        SymbolIterator it = currentProgram.getSymbolTable().getAllSymbols(true);
        while (it.hasNext() && symbols.size() < 20000) {
            Symbol s = it.next();
            symbols.add(s.getAddress() + "  " + s.getName());
        }
        meta.println("=== SYMBOLS (" + symbols.size() + ") ===");
        for (String s : symbols) {
            meta.println(s);
        }
        meta.close();

        // 1. Ensure all discovered functions and any unmapped entry points / symbols are registered
        FunctionManager fm = currentProgram.getFunctionManager();
        int initialCount = fm.getFunctionCount();
        println("DecompileAll: Functions found by auto-analysis: " + initialCount);

        int txId = currentProgram.startTransaction("DecompileAll Ensure Functions");
        try {
            // A. External Entry Points
            AddressIterator entryPoints = currentProgram.getSymbolTable().getExternalEntryPointIterator();
            while (entryPoints.hasNext()) {
                Address ep = entryPoints.next();
                if (fm.getFunctionAt(ep) == null && fm.getFunctionContaining(ep) == null) {
                    try {
                        createFunction(ep, null);
                    } catch (Exception ignored) {}
                }
            }

            // B. Exported and local function symbols in executable memory (skip generic labels to prevent memory dump bloat)
            SymbolIterator symIt = currentProgram.getSymbolTable().getAllSymbols(true);
            while (symIt.hasNext()) {
                Symbol s = symIt.next();
                Address addr = s.getAddress();
                MemoryBlock block = currentProgram.getMemory().getBlock(addr);
                if (block != null && block.isExecute() && !block.isOverlay()) {
                    if (s.getSymbolType() == SymbolType.FUNCTION) {
                        if (fm.getFunctionAt(addr) == null && fm.getFunctionContaining(addr) == null) {
                            try {
                                createFunction(addr, s.getName());
                            } catch (Exception ignored) {}
                        }
                    }
                }
            }
        } finally {
            currentProgram.endTransaction(txId, true);
        }

        List<Function> funcs = new ArrayList<>();
        for (Function f : fm.getFunctions(true)) {
            if (!f.isExternal()) {
                funcs.add(f);
            }
        }
        funcs.sort((a, b) -> a.getEntryPoint().compareTo(b.getEntryPoint()));
        println("DecompileAll: Total functions to decompile: " + funcs.size());

        // 2. Configure the decompiler. Each worker thread gets its own
        // DecompInterface because a single instance is not thread-safe.
        ghidra.app.decompiler.DecompileOptions options = new ghidra.app.decompiler.DecompileOptions();
        try {
            options.grabFromProgram(currentProgram);
        } catch (Exception ignored) {}

        int funcTimeout = 3;
        try {
            String ft = System.getenv("DECOMP_FUNC_TIMEOUT");
            if (ft != null && !ft.trim().isEmpty()) {
                funcTimeout = Math.max(1, Integer.parseInt(ft.trim()));
            }
        } catch (NumberFormatException ignored) {}
        options.setDefaultTimeout(funcTimeout);

        int workers = 0;
        try {
            String we = System.getenv("DECOMP_WORKERS");
            if (we != null && !we.trim().isEmpty()) {
                workers = Integer.parseInt(we.trim());
            }
        } catch (NumberFormatException ignored) {}
        if (workers <= 0) {
            workers = Math.max(1, Math.min(4, Runtime.getRuntime().availableProcessors()));
        }

        PrintWriter out = new PrintWriter(new FileWriter(cFile));
        out.println("/*");
        out.println(" * Ghidra decompiled output");
        out.println(" * File: " + currentProgram.getName());
        out.println(" * Functions: " + funcs.size());
        out.println(" */");
        out.println("");

        int total = funcs.size();
        workers = Math.min(workers, Math.max(1, total));
        final int workerCount = workers;
        final AtomicInteger done = new AtomicInteger(0);
        final AtomicInteger partial = new AtomicInteger(0);
        final Object progressLock = new Object();
        println("DECOMP_PROGRESS 0/" + total + " workers=" + workerCount);

        if (workers <= 1) {
            DecompInterface decomp = new DecompInterface();
            decomp.setOptions(options);
            decomp.openProgram(currentProgram);
            int sinceReset = 0;
            try {
                for (Function f : funcs) {
                    if (budgetMs > 0 && System.currentTimeMillis() - startMs >= budgetMs) {
                        out.println("/* [PARTIAL] time budget of " + (budgetMs / 1000)
                                + "s reached after " + done.get() + "/" + total + " functions */");
                        out.flush();
                        println("DECOMP_PARTIAL " + done.get() + "/" + total);
                        partial.set(1);
                        break;
                    }
                    if (sinceReset >= 800) {
                        decomp.dispose();
                        decomp = new DecompInterface();
                        decomp.setOptions(options);
                        decomp.openProgram(currentProgram);
                        sinceReset = 0;
                        System.gc();
                    }
                    out.println("// ---------- " + f.getName() + " @ " + f.getEntryPoint() + " ----------");
                    out.println(decompileOne(decomp, f, funcTimeout));
                    out.println("");
                    int d = done.incrementAndGet();
                    sinceReset++;
                    if (d % 5 == 0 || d == total) {
                        out.flush();
                        println("DECOMP_PROGRESS " + d + "/" + total);
                    }
                    if (d % 200 == 0) {
                        println("DECOMP_HEARTBEAT " + d + "/" + total
                                + " elapsed=" + ((System.currentTimeMillis() - startMs) / 1000) + "s");
                    }
                }
            } finally {
                decomp.dispose();
            }
        } else {
            ExecutorService pool = Executors.newFixedThreadPool(workerCount);
            AtomicInteger next = new AtomicInteger(0);
            List<Future<?>> futures = new ArrayList<>();
            for (int w = 0; w < workerCount; w++) {
                futures.add(pool.submit(() -> {
                    DecompInterface dec = new DecompInterface();
                    dec.setOptions(options);
                    dec.openProgram(currentProgram);
                    int sinceReset = 0;
                    try {
                        while (true) {
                            if (budgetMs > 0 && System.currentTimeMillis() - startMs >= budgetMs) {
                                partial.set(1);
                                break;
                            }
                            int i = next.getAndIncrement();
                            if (i >= total) {
                                break;
                            }
                            Function f = funcs.get(i);
                            if (sinceReset >= 800) {
                                dec.dispose();
                                dec = new DecompInterface();
                                dec.setOptions(options);
                                dec.openProgram(currentProgram);
                                sinceReset = 0;
                            }
                            String text = decompileOne(dec, f, funcTimeout);
                            synchronized (out) {
                                out.println("// ---------- " + f.getName() + " @ " + f.getEntryPoint() + " ----------");
                                out.println(text);
                                out.println("");
                            }
                            int d = done.incrementAndGet();
                            sinceReset++;
                            if (d % 5 == 0 || d == total) {
                                synchronized (out) {
                                    out.flush();
                                }
                                synchronized (progressLock) {
                                    println("DECOMP_PROGRESS " + d + "/" + total);
                                    if (d % 200 == 0) {
                                        println("DECOMP_HEARTBEAT " + d + "/" + total
                                                + " elapsed=" + ((System.currentTimeMillis() - startMs) / 1000) + "s");
                                    }
                                }
                            }
                        }
                    } finally {
                        dec.dispose();
                    }
                }));
            }
            for (Future<?> fut : futures) {
                try {
                    fut.get();
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    break;
                } catch (ExecutionException ee) {
                    synchronized (progressLock) {
                        println("DECOMP_WORKER_ERROR " + ee.getCause());
                    }
                }
            }
            pool.shutdown();
            if (partial.get() == 1) {
                synchronized (out) {
                    out.println("/* [PARTIAL] time budget of " + (budgetMs / 1000)
                            + "s reached after " + done.get() + "/" + total + " functions */");
                    out.flush();
                }
                println("DECOMP_PARTIAL " + done.get() + "/" + total);
            }
        }
        out.close();

        println("DONE: wrote " + cFile + " with " + done.get() + " functions");
    }

    /** Decompile a single function without touching any shared writer state. */
    private static String decompileOne(DecompInterface decomp, Function f, int funcTimeout) {
        if (f.isThunk()) {
            Function thunked = f.getThunkedFunction(true);
            return "// [THUNK] jumps to " + (thunked != null ? thunked.getName() : "unknown");
        }
        if (f.getBody().getNumAddresses() <= 4) {
            return "// [STUB] empty / trivial function";
        }
        DecompileResults res = decomp.decompileFunction(f, funcTimeout, null);
        if (res != null && res.decompileCompleted()) {
            return res.getDecompiledFunction().getC();
        }
        String errMsg = (res != null && res.getErrorMessage() != null) ? " (" + res.getErrorMessage() + ")" : "";
        return "/* [FAILED] could not decompile " + f.getName() + errMsg + " */";
    }
}
