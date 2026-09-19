# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Lei Zhou, Linaro
cmake_minimum_required(VERSION 3.16)
project(mcib C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
add_compile_options(-Wall -Wextra)

# A build with no configuration named is a MEASUREMENT build.
#
# The reentrancy check in the hot path is an assertion, and an assertion left
# in a measurement build lands inside the interval being measured and inside
# the instrument's calibrated cost. The default therefore defines NDEBUG,
# rather than leaving it to whoever remembers to pass it.
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING
      "Build configuration: Release measures, Debug checks" FORCE)
  set_property(CACHE CMAKE_BUILD_TYPE PROPERTY STRINGS Release Debug)
endif()

# Both configurations compile at the same optimisation level on purpose. The
# difference between them is meant to be the assertion and nothing else, so
# that the calibrated per-event inflation of one can be compared against the
# other and the difference attributed to the check rather than to the
# optimiser. -O2 also matches the flags the target binaries are built with.
set(CMAKE_C_FLAGS_RELEASE "-O2 -DNDEBUG"
    CACHE STRING "measurement build: assertions compiled out" FORCE)
set(CMAKE_C_FLAGS_DEBUG   "-O2 -g"
    CACHE STRING "checking build: reentrancy assertion live" FORCE)

# Core: the probe library. This target must build and be useful on its own,
# with no victim present — a victim depends on the library, never the reverse.
add_library(mcib_probe STATIC
    src/probe.c
    src/time_source.c
    src/events_arm.c
    src/backend_linux_perf.c)
target_include_directories(mcib_probe PUBLIC include PRIVATE src)
target_link_libraries(mcib_probe PUBLIC pthread)

# Victims — not part of the core library. Each depends on the library; the
# library depends on none of them.
add_executable(imlat victims/imlat.c)
target_link_libraries(imlat PRIVATE mcib_probe)

add_executable(tslat victims/tslat.c)
target_link_libraries(tslat PRIVATE mcib_probe)

add_executable(prelat victims/prelat.c)
target_link_libraries(prelat PRIVATE mcib_probe)
